# 服务器CPU飙高排查手册

| 文档属性 | 内容 |
| --- | --- |
| 文档分类 | troubleshooting |
| 文档版本 | v1.8 |
| 适用对象 | SRE、后端开发、运维 |
| 维护人 | SRE 组 |

## 1. 故障现象

### 1.1 告警通知

监控系统（Prometheus + Grafana）触发 CPU 告警：

```
[告警] prod-payment-node-03 CPU 使用率 > 90% 持续 3 分钟
当前值: 96.2%, 阈值: 90%
节点: 10.0.1.13
时间: 2026-07-31 14:23:18
```

### 1.2 影响评估

CPU 飙高会引发连锁反应，需立即评估：

| 影响层级 | 表现 | 风险 |
| --- | --- | --- |
| 接口层 | 响应时间 P99 上升、超时增多 | 用户体验差、调用方熔断 |
| 应用层 | 请求队列堆积、线程池耗尽 | 拒绝服务 |
| 依赖层 | 数据库连接池占满、下游被打爆 | 级联故障 |
| 系统层 | 负载升高、上下文切换频繁 | 整机卡死 |

## 2. 排查步骤

### 2.1 确认告警真实性

首先确认告警是否为瞬时抖动或误报：

```bash
# 查看当前负载
uptime
# 输出示例
# 14:25:01 up 120 days,  3:21,  3 users,  load average: 16.82, 12.10, 6.05
# load average 三组数字分别是 1/5/15 分钟平均负载
# 单核 CPU 负载 > 1 即满载，16 核 CPU 负载 > 16 为过载
```

```bash
# 查看 CPU 使用率（按核心）
mpstat -P ALL 1 3
```

### 2.2 top 查看整体 CPU

```bash
top
```

关键输出：

```
top - 14:25:30 up 120 days,  3:21,  3 users,  load average: 16.82, 12.10, 6.05
Tasks: 287 total,   2 running, 285 sleeping,   0 stopped,   0 zombie
%Cpu(s): 95.3 us,  2.1 sy,  0.0 ni,  1.5 id,  0.0 wa,  0.8 hi,  0.3 si,  0.0 st

  PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND
12345 app       20   0  4.2g   1.8g   12m S 185.6  4.5  120:45 java
12346 app       20   0  2.1g   980m   8m  S  95.2  2.4   58:23 java
 1122 root      20   0  500m   80m    10m S  12.3  0.2   1:05  nginx
```

关注点：

- `%Cpu(s)` 行中 `us`（用户态）高，说明应用消耗 CPU；`sy`（内核态）高说明系统调用/上下文切换开销大
- `load average` 远超 CPU 核数，说明过载
- 找到 `%CPU` 最高的进程，这里是 PID 12345 的 java 进程

### 2.3 定位高 CPU 进程

```bash
# 按 CPU 排序查看前 10 进程
ps -eo pid,user,%cpu,%mem,cmd --sort=-%cpu | head -10
```

### 2.4 top -H -p 定位高 CPU 线程

```bash
# 查看进程 12345 内的线程 CPU 占用
top -H -p 12345
```

输出示例：

```
  PID USER      PR  NI    VIRT    RES    SHR S %CPU  %MEM     TIME+ COMMAND
12378 app       20   0  4.2g   1.8g   12m R 98.5  4.5   8:23  java
12379 app       20   0  4.2g   1.8g   12m R 87.2  4.5   7:15  java
12380 app       20   0  4.2g   1.8g   12m R 76.8  4.5   6:42  java
```

找到占用 CPU 最高的线程 PID，这里是 12378。

### 2.5 分析线程堆栈

将线程 PID（十进制）转换为十六进制：

```bash
printf "%x\n" 12378
# 输出: 305a
```

使用 jstack 抓取该线程的堆栈：

```bash
# 抓取 3 次堆栈，间隔 1 秒
jstack 12345 > /tmp/jstack1.txt
sleep 1
jstack 12345 > /tmp/jstack2.txt

# 在堆栈中查找 nid=0x305a 的线程
grep "nid=0x305a" /tmp/jstack1.txt -A 30
```

### 2.6 检查系统负载和上下文切换

```bash
# 查看上下文切换次数
vmstat 1 5
# 输出中 cs 列为每秒上下文切换次数，> 10万 需关注

# 查看中断
watch -n 1 "cat /proc/interrupts | head -10"
```

## 3. Java 应用排查

### 3.1 jstack 线程栈分析

```bash
# 导出完整线程栈
jstack -l 12345 > /tmp/jstack_full.txt
```

线程状态解读：

```
"http-nio-8080-exec-3" #25 daemon prio=5 os_prio=0 tid=0x00007f8a0c0a8800 nid=0x305a runnable [0x00007f89f8001000]
   java.lang.Thread.State: RUNNABLE
        at com.example.order.service.OrderService.calculatePrice(OrderService.java:128)
        at com.example.order.controller.OrderController.createOrder(OrderController.java:45)
        ...
```

线程状态含义：

| 状态 | 含义 | 处理建议 |
| --- | --- | --- |
| RUNNABLE | 正在执行或等待 CPU | 持续占用 CPU，需看堆栈定位代码 |
| BLOCKED | 等待获取 monitor 锁 | 检查锁竞争，可能死锁 |
| WAITING | 无限期等待 | 一般正常，如线程池空闲线程 |
| TIMED_WAITING | 限时等待 | 一般正常，如 sleep/wait(timeout) |

若多个线程 BLOCKED 在同一对象，需检查死锁：

```bash
jstack -l 12345 | grep -A 5 "Found .* deadlock"
```

### 3.2 jmap 内存分析

CPU 飙高常伴随频繁 GC，需检查堆内存：

```bash
# 查看 GC 情况
jstat -gcutil 12345 1000 5
# 输出 S0 S1 E O M YGC YGCT FGC FGCT GCT
# 0.00 98.44 100.00 99.87 95.42 234 3.456 45 12.345 15.801
# FGC（Full GC 次数）快速增长说明内存不足，GC 线程占用 CPU

# 导出堆 dump 供离线分析
jmap -dump:format=b,file=/tmp/heap.hprof 12345
```

### 3.3 GC 日志分析

```bash
# 查看 GC 日志（JVM 启动参数中需配置 -Xlog:gc*）
tail -f /var/log/app/gc.log
```

异常日志示例：

```
[2026-07-31T14:25:01.123+0800] GC(1024) Pause Full (G1 Compaction Pause)
  Live-Regions: 1024 -> 1024, Live-Bytes: 17179869184 -> 17179869184
  User=8.23s Sys=0.15s Real=8.45s
```

Full GC 持续 8 秒且内存没回收，说明内存泄漏或大对象驻留。

### 3.4 arthas 在线诊断

arthas 是排查 Java 问题的利器，无需重启应用：

```bash
# 启动 arthas
java -jar arthas-boot.jar 12345
```

常用命令：

```bash
# 查看整体面板
dashboard

# 查看 CPU 占用最高的线程
thread -n 5

# 查看某线程的堆栈
thread 25

# 追踪方法调用链路耗时
trace com.example.order.service.OrderService calculatePrice

# 查看方法入参出参
watch com.example.order.service.OrderService calculatePrice "{params, returnObj}" -x 2

# 反编译类
jad com.example.order.service.OrderService

# 查看方法调用次数和耗时统计
monitor com.example.order.service.OrderService calculatePrice -c 10
```

`thread -n 5` 输出示例：

```
threads from 12345
          tname            threadId       cpu      pcnt  state          time            interrupt
          http-nio-8080-e   25             98.5     98.5  RUNNABLE       8:23            false
          http-nio-8080-e   26             87.2     87.2  RUNNABLE       7:15            false
```

`trace` 输出示例：

```
`---[99.9% 123ms ] com.example.order.service.OrderService:calculatePrice()
    +---[0.1%  0.1ms] com.example.order.util.PriceUtil:getBasePrice()
    +---[98.5% 121ms] com.example.order.util.DiscountUtil:calcDiscount()  # 这里耗时最高
    `---[1.3%  1.6ms] com.example.order.util.TaxUtil:calcTax()
```

立即定位到 `DiscountUtil.calcDiscount()` 是耗时大头。

## 4. Python 应用排查

### 4.1 py-spy

无需修改代码，对生产应用影响极小：

```bash
# 安装
pip install py-spy

# 查看进程 CPU 占用最高的函数
py-spy top --pid 12345

# dump 调用栈
py-spy dump --pid 12345

# 生成火焰图
py-spy record --pid 12345 -o profile.svg --duration 30
```

### 4.2 cProfile

需要在代码中埋点或使用启动参数：

```python
import cProfile
import pstats

profiler = cProfile.Profile()
profiler.enable()

# 业务代码
run_business_logic()

profiler.disable()
stats = pstats.Stats(profiler).sort_stats('cumulative')
stats.print_stats(20)
```

## 5. Go 应用排查

### 5.1 pprof

```bash
# 查看堆栈 CPU profile（应用需引入 net/http/pprof）
go tool pprof http://10.0.1.13:6060/debug/pprof/profile?seconds=30

# pprof 交互命令
(pprof) top 10
(pprof) list <function_name>
(pprof) web  # 生成调用图（需 graphviz）
```

### 5.2 trace

```bash
# 抓取执行 trace
curl -o trace.out http://10.0.1.13:6060/debug/pprof/trace?seconds=10
go tool trace trace.out
```

## 6. 常见根因

| 根因 | 典型场景 | 排查方法 |
| --- | --- | --- |
| 死循环 | while 循环条件错误、递归无终止 | jstack 多次抓栈确认 |
| 正则回溯 | 复杂正则匹配长字符串 | 堆栈出现 `java.util.regex` |
| 大对象 GC | 内存泄漏导致频繁 Full GC | jstat -gcutil |
| 线程竞争 | 大量线程 BLOCKED 在锁 | jstack 看 BLOCKED 线程 |
| 加密计算 | 高频 RSA/AES 运算 | 堆栈定位加密库 |
| 日志风暴 | 异常导致大量 ERROR 日志 | 查看日志输出速率 |
| 序列化 | 大对象 JSON 序列化 | trace 定位 |
| 加密货币挖矿木马 | 服务器被入侵 | top 看可疑进程 |

## 7. 应急处理

按风险优先级处理：

### 7.1 限流降级

```bash
# 通过配置中心动态调小限流阈值
# Sentinel 规则调整 QPS 上限
curl -X POST http://config-center/api/rule -d '{"resource":"createOrder","count":50}'
```

### 7.2 扩容

```bash
# K8s 扩容
kubectl scale deployment payment --replicas=10 -n prod

# 触发 HPA
kubectl autoscale deployment payment --cpu-percent=60 --min=4 --max=20 -n prod
```

### 7.3 重启

作为最后手段，重启问题实例：

```bash
# 优雅重启
kubectl delete pod payment-abc123 -n prod
```

## 8. 根因修复和预防

### 8.1 代码优化示例

**死循环案例：**

```java
// 错误：条件判断错误导致死循环
while (pageNo < totalPages) {
    processPage(pageNo);
    // 忘记 pageNo++，导致死循环
}

// 正确
while (pageNo < totalPages) {
    processPage(pageNo);
    pageNo++;
}
```

**正则回溯案例：**

```python
import re

# 错误： catastrophic backtracking
pattern = r"(a+)+b"
re.match(pattern, "a" * 30)  # CPU 飙高

# 正确：使用原子组或优化正则
pattern = r"a+b"
# 或使用 re2 库（无回溯）
import google_re2 as re
```

### 8.2 监控告警

| 指标 | 阈值 | 告警级别 |
| --- | --- | --- |
| CPU 使用率 | > 80% 持续 3 分钟 | P2 |
| Load average | > CPU 核数 | P2 |
| 上下文切换 | > 10万/秒 | P3 |
| GC 耗时占比 | > 10% | P2 |
| 接口 P99 | > 500ms | P2 |

### 8.3 压测验证

每次大版本发布前在预发环境做全链路压测，使用 Wrk 或 JMeter 模拟峰值流量：

```bash
# wrk 压测示例
wrk -t8 -c500 -d60s --latency http://api.example.com/order/create
```

关注压测中 CPU 是否线性增长、是否出现热点函数，提前发现性能瓶颈。
