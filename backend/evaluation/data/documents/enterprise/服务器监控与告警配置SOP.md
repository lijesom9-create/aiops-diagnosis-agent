# 服务器监控与告警配置 SOP

| 文档属性 | 内容 |
| --- | --- |
| 文档编号 | SOP-OPS-MONITOR-004 |
| 分类 | ops_sop |
| 版本 | v2.5 |
| 适用范围 | 公司生产环境主机、容器、应用、业务全链路监控告警体系 |
| 维护人 | SRE 组 - 刘洋 |
| 生效日期 | 2024-04-15 |
| 审核人 | 运维总监 李伟 |

---

## 1. 目的与适用范围

本 SOP 规范公司基于 Prometheus + Grafana + Alertmanager 的监控告警体系建设，覆盖监控指标采集、Dashboard 设计、告警分级、通知渠道、告警处理全流程，目标做到"故障 1 分钟发现、3 分钟定位、10 分钟恢复"。

适用于所有生产、预发、测试环境的服务器、Kubernetes 集群、中间件、业务应用。

## 2. 监控体系架构

### 2.1 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                       被监控目标                              │
│  主机(node_exporter)  容器(cAdvisor)  应用(/metrics)         │
│  MySQL(mysqld_exporter) Redis(redis_exporter)               │
│  Kafka(JMX)  Nginx(nginx_exporter)  Blackbox(HTTP探针)      │
└───────────────────────────┬─────────────────────────────────┘
                            │ Pull /metrics
                            ↓
┌─────────────────────────────────────────────────────────────┐
│                    Prometheus 集群                            │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐       │
│  │ Prometheus A │  │ Prometheus B │  │ Prometheus C │       │
│  │  (主机/基础)  │  │  (应用/业务)  │  │  (网络/中间件)│       │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘       │
│         └──────────┬──────┴─────────────────┘               │
│                    │ Federation                             │
│                    ↓                                        │
│             ┌──────────────┐                                │
│             │ Thanos Query │   ← 全局查询入口                │
│             └──────┬───────┘                                │
└────────────────────┼────────────────────────────────────────┘
                     │
        ┌────────────┼────────────┐
        ↓            ↓            ↓
   ┌─────────┐  ┌─────────┐  ┌──────────────┐
   │ Grafana │  │  Alert  │  │  长期存储      │
   │  展示    │  │ manager │  │  Thanos/OSS   │
   └─────────┘  └────┬────┘  └──────────────┘
                     │
                     ↓
   ┌─────────────────────────────────────┐
   │   钉钉 / 企微 / 邮件 / 电话 / SMS    │
   └─────────────────────────────────────┘
```

### 2.2 组件职责

| 组件 | 职责 | 部署方式 | 版本 |
| --- | --- | --- | --- |
| Prometheus | 指标采集、存储、告警评估 | k8s StatefulSet | v2.45 |
| Thanos | 全局查询、长期存储、降采样 | k8s Deployment | v0.32 |
| Grafana | 可视化展示 | k8s Deployment | v10.0 |
| Alertmanager | 告警路由、抑制、静默、通知 | k8s StatefulSet | v0.26 |
| node_exporter | 主机指标采集 | DaemonSet / systemd | v1.6 |
| cAdvisor | 容器指标采集 | DaemonSet | v0.47 |

## 3. 监控指标分类

### 3.1 USE 原则（资源）/ RED 原则（服务）

- **USE**（Utilization / Saturation / Errors）：用于资源类指标
- **RED**（Rate / Errors / Duration）：用于服务类指标

### 3.2 指标分层

| 层级 | 指标类别 | 关键指标 | 采集源 | 频率 |
| --- | --- | --- | --- | --- |
| L1 | 主机指标 | CPU、内存、磁盘、网络、负载、inode | node_exporter | 15s |
| L2 | 容器指标 | CPU、内存、网络、磁盘 IO、重启次数 | cAdvisor | 15s |
| L3 | Kubernetes | Pod 状态、Deployment 副本数、节点状态 | kube-state-metrics | 30s |
| L4 | 中间件 | MySQL QPS、Redis 命中率、Kafka 消费延迟 | 各 exporter | 15s |
| L5 | 应用指标 | QPS、错误率、P99 延迟、线程池、GC | Micrometer | 15s |
| L6 | 业务指标 | 下单量、支付成功率、注册数、库存 | 业务埋点 | 30s |
| L7 | 日志指标 | ERROR 日志频次、关键异常计数 | Promtail + Loki | 1m |
| L8 | 合成监控 | 外部 HTTP 探测、SSL 证书、DNS | Blackbox Exporter | 30s |

## 4. Prometheus 配置

### 4.1 完整 prometheus.yml 示例

```yaml
# /etc/prometheus/prometheus.yml
global:
  scrape_interval: 15s
  scrape_timeout: 10s
  evaluation_interval: 15s
  external_labels:
    cluster: 'prod-bj'
    replica: 'A'

# 告警规则文件
rule_files:
  - /etc/prometheus/rules/host.yml
  - /etc/prometheus/rules/kubernetes.yml
  - /etc/prometheus/rules/middleware.yml
  - /etc/prometheus/rules/business.yml
  - /etc/prometheus/rules/blackbox.yml

# 告警推送到 Alertmanager
alerting:
  alertmanagers:
    - static_configs:
        - targets:
            - alertmanager-0.alertmanager:9093
            - alertmanager-1.alertmanager:9093
      timeout: 10s

# 采集配置
scrape_configs:
  # 1. Prometheus 自身
  - job_name: 'prometheus'
    static_configs:
      - targets: ['localhost:9090']

  # 2. 主机指标（node_exporter）
  - job_name: 'node'
    file_sd_configs:
      - files: ['/etc/prometheus/sd/nodes.yml']
        refresh_interval: 30s
    relabel_configs:
      - source_labels: [__address__]
        regex: '(.*):9100'
        target_label: instance
        replacement: '${1}'
      - source_labels: [__meta_file_sd_label_region]
        target_label: region

  # 3. Kubernetes Pod 自动发现
  - job_name: 'kubernetes-pods'
    kubernetes_sd_configs:
      - role: pod
    relabel_configs:
      - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_scrape]
        regex: 'true'
        action: keep
      - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_path]
        regex: (.+)
        target_label: __metrics_path__
      - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_port, __meta_kubernetes_pod_ip]
        regex: (.+);(.+)
        target_label: __address__
        replacement: $2:$1
      - source_labels: [__meta_kubernetes_namespace]
        target_label: namespace
      - source_labels: [__meta_kubernetes_pod_name]
        target_label: pod
      - source_labels: [__meta_kubernetes_pod_label_app]
        target_label: app

  # 4. Kubernetes 节点
  - job_name: 'kubernetes-nodes'
    kubernetes_sd_configs:
      - role: node
    relabel_configs:
      - source_labels: [__address__]
        regex: '(.*):10250'
        target_label: __address__
        replacement: '${1}:9100'

  # 5. 中间件 - MySQL
  - job_name: 'mysql'
    static_configs:
      - targets: ['mysql-exporter:9104']
        labels:
          cluster: 'prod-mysql'

  # 6. Blackbox HTTP 探针
  - job_name: 'blackbox-http'
    metrics_path: /probe
    params:
      module: [http_2xx]
    file_sd_configs:
      - files: ['/etc/prometheus/sd/blackbox.yml']
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: blackbox-exporter:9115

  # 7. 业务指标
  - job_name: 'business'
    metrics_path: /actuator/prometheus
    kubernetes_sd_configs:
      - role: pod
    relabel_configs:
      - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_scrape_business]
        regex: 'true'
        action: keep

# 远程写入 Thanos
remote_write:
  - url: http://thanos-receive:19291/api/v1/receive
    queue_config:
      capacity: 10000
      max_samples_per_send: 2000
      max_shards: 200
```

### 4.2 Recording Rules（预聚合）

```yaml
# /etc/prometheus/rules/recording.yml
groups:
  - name: node-recording
    interval: 30s
    rules:
      - record: node:cpu_usage:ratio
        expr: |
          1 - avg by (instance) (
            rate(node_cpu_seconds_total{mode="idle"}[5m])
          )

      - record: node:memory_usage:ratio
        expr: |
          1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)

      - record: node:disk_usage:ratio
        expr: |
          1 - (node_filesystem_avail_bytes{fstype!~"tmpfs|overlay"}
               / node_filesystem_size_bytes{fstype!~"tmpfs|overlay"})

      - record: node:load1:ratio
        expr: node_load1 / count by (instance) (node_cpu_seconds_total{mode="idle"})

  - name: http-recording
    interval: 30s
    rules:
      - record: http:request_rate5m
        expr: sum by (app, status) (rate(http_requests_total[5m]))

      - record: http:error_rate5m
        expr: |
          sum by (app) (rate(http_requests_total{status=~"5.."}[5m]))
          /
          sum by (app) (rate(http_requests_total[5m]))

      - record: http:p99_latency5m
        expr: |
          histogram_quantile(0.99,
            sum by (app, le) (rate(http_request_duration_seconds_bucket[5m])))
```

## 5. 告警规则配置

### 5.1 主机告警规则

```yaml
# /etc/prometheus/rules/host.yml
groups:
  - name: host-alerts
    interval: 30s
    rules:
      # CPU 持续高负载
      - alert: HighCpuUsage
        expr: node:cpu_usage:ratio > 0.85
        for: 5m
        labels:
          severity: warning
          team: sre
        annotations:
          summary: "主机 CPU 使用率过高 {{ $labels.instance }}"
          description: "CPU 使用率 {{ $value | humanizePercentage }} 持续 5 分钟超过 85%"
          runbook: "https://wiki.example.com/runbook/high-cpu"

      # 内存使用率高
      - alert: HighMemoryUsage
        expr: node:memory_usage:ratio > 0.90
        for: 5m
        labels:
          severity: warning
          team: sre
        annotations:
          summary: "主机内存使用率过高 {{ $labels.instance }}"
          description: "内存使用率 {{ $value | humanizePercentage }} 超过 90%"

      # 磁盘空间不足
      - alert: DiskSpaceLow
        expr: node:disk_usage:ratio > 0.85
        for: 10m
        labels:
          severity: warning
          team: sre
        annotations:
          summary: "磁盘空间不足 {{ $labels.instance }}"
          description: "挂载点 {{ $labels.mountpoint }} 使用率 {{ $value | humanizePercentage }}"

      - alert: DiskSpaceCritical
        expr: node:disk_usage:ratio > 0.95
        for: 2m
        labels:
          severity: critical
          team: sre
        annotations:
          summary: "磁盘空间严重不足 {{ $labels.instance }}"

      # 磁盘 IO 高
      - alert: HighDiskIO
        expr: rate(node_disk_io_time_seconds_total[5m]) > 0.8
        for: 10m
        labels:
          severity: warning

      # 节点宕机
      - alert: NodeDown
        expr: up{job="node"} == 0
        for: 1m
        labels:
          severity: critical
          team: sre
        annotations:
          summary: "主机宕机 {{ $labels.instance }}"
          description: "node_exporter 失联超过 1 分钟"

      # 文件描述符不足
      - alert: FileDescriptorsHigh
        expr: node_filefd_allocated / node_filefd_maximum > 0.8
        for: 5m
        labels:
          severity: warning

      # inode 耗尽
      - alert: InodesLow
        expr: 1 - (node_filesystem_files_free / node_filesystem_files) > 0.85
        for: 10m
        labels:
          severity: warning
```

### 5.2 应用与 HTTP 告警规则

```yaml
# /etc/prometheus/rules/app.yml
groups:
  - name: app-alerts
    interval: 30s
    rules:
      # HTTP 5xx 错误率
      - alert: HighHttpErrorRate
        expr: http:error_rate5m > 0.05
        for: 3m
        labels:
          severity: critical
          team: app
        annotations:
          summary: "{{ $labels.app }} HTTP 5xx 错误率高"
          description: "5xx 错误率 {{ $value | humanizePercentage }} 超过 5%"

      # P99 响应时间
      - alert: HighLatencyP99
        expr: http:p99_latency5m > 1
        for: 5m
        labels:
          severity: warning
          team: app
        annotations:
          summary: "{{ $labels.app }} P99 响应时间过高"
          description: "P99 延迟 {{ $value }}s 超过 1s"

      # Pod 重启
      - alert: PodRestart
        expr: increase(kube_pod_container_status_restarts_total[1h]) > 3
        for: 1m
        labels:
          severity: warning
          team: app
        annotations:
          summary: "Pod 频繁重启 {{ $labels.pod }}"

      # Pod 非 Running
      - alert: PodNotRunning
        expr: kube_pod_status_phase{phase!="Running"} == 1
        for: 5m
        labels:
          severity: critical

      # Deployment 副本不足
      - alert: DeploymentReplicasMismatch
        expr: |
          kube_deployment_spec_replicas
            != kube_deployment_status_available_replicas
        for: 10m
        labels:
          severity: warning

      # JVM GC 时间过长
      - alert:JvmGcPauseLong
        expr: increase(jvm_gc_pause_seconds_sum[5m]) > 10
        for: 5m
        labels:
          severity: warning

      # JVM 堆使用率高
      - alert: JvmHeapHigh
        expr: jvm_memory_used_bytes{area="heap"} / jvm_memory_max_bytes{area="heap"} > 0.85
        for: 5m
        labels:
          severity: warning
```

### 5.3 业务与黑盒告警

```yaml
# /etc/prometheus/rules/business.yml
groups:
  - name: business-alerts
    rules:
      - alert: OrderCreateRateDrop
        expr: |
          sum(rate(order_created_total[10m]))
            < sum(rate(order_created_total[10m] offset 1h)) * 0.5
        for: 10m
        labels:
          severity: critical
          team: business
        annotations:
          summary: "下单速率较 1 小时前下降 50%"

      - alert: PaymentSuccessRateLow
        expr: |
          sum(rate(payment_success_total[5m]))
            / sum(rate(payment_attempt_total[5m])) < 0.95
        for: 5m
        labels:
          severity: critical
          team: business

      - alert: KafkaConsumerLag
        expr: kafka_consumergroup_lag > 10000
        for: 10m
        labels:
          severity: warning

# /etc/prometheus/rules/blackbox.yml
groups:
  - name: blackbox-alerts
    rules:
      - alert: HttpProbeFailed
        expr: probe_success == 0
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "HTTP 探测失败 {{ $labels.instance }}"

      - alert: SslCertExpiring
        expr: probe_ssl_earliest_cert_expiry - time() < 86400 * 14
        for: 1h
        labels:
          severity: warning
        annotations:
          summary: "SSL 证书 14 天内过期 {{ $labels.instance }}"

      - alert: SslCertExpired
        expr: probe_ssl_earliest_cert_expiry - time() < 86400 * 3
        for: 1h
        labels:
          severity: critical
```

## 6. Alertmanager 告警路由配置

### 6.1 完整 alertmanager.yml

```yaml
# /etc/alertmanager/alertmanager.yml
global:
  resolve_timeout: 5m
  # SMTP 配置（邮件通知）
  smtp_smarthost: 'smtp.company.com:465'
  smtp_from: 'alert@company.com'
  smtp_auth_username: 'alert@company.com'
  smtp_auth_password: 'REDACTED'
  smtp_require_tls: false

# 告警模板
templates:
  - '/etc/alertmanager/templates/*.tmpl'

# 路由配置
route:
  group_by: ['alertname', 'cluster', 'app']
  group_wait: 30s           # 同组告警等待 30 秒合并
  group_interval: 5m        # 同组下次发送间隔
  repeat_interval: 4h       # 未解决告警每 4 小时重复
  receiver: 'default-dingtalk'

  routes:
    # P1 紧急 - 直接电话 + 钉钉 + 邮件
    - matchers:
        - severity = "critical"
      receiver: 'p1-escalation'
      group_wait: 0s
      repeat_interval: 30m
      routes:
        - matchers:
            - team = "sre"
          receiver: 'sre-p1'

    # P2 重要 - 钉钉 + 邮件
    - matchers:
        - severity = "warning"
      receiver: 'team-dingtalk'
      repeat_interval: 2h
      routes:
        - matchers:
            - team = "app"
          receiver: 'app-team-dingtalk'
        - matchers:
            - team = "business"
          receiver: 'biz-team-dingtalk'

    # 业务告警特殊处理
    - matchers:
        - alertname =~ "OrderCreateRateDrop|PaymentSuccessRateLow"
      receiver: 'business-call'
      group_wait: 0s

# 抑制规则
inhibit_rules:
  # 节点宕机时，抑制该节点上的其他告警
  - source_matchers:
      - alertname = "NodeDown"
    target_matchers:
      - severity =~ "warning|critical"
    equal: ['instance']

  # Pod 不存在时，抑制 Pod 告警
  - source_matchers:
      - alertname = "PodNotRunning"
    target_matchers:
      - alertname =~ "HighHttpErrorRate|HighLatencyP99"
    equal: ['pod']

  # 磁盘 95% 时，抑制 85% 的告警
  - source_matchers:
      - alertname = "DiskSpaceCritical"
    target_matchers:
      - alertname = "DiskSpaceLow"
    equal: ['instance', 'mountpoint']

# 接收器
receivers:
  - name: 'default-dingtalk'
    webhook_configs:
      - url: 'https://oapi.dingtalk.com/robot/send?access_token=DEFAULT_TOKEN'
        send_resolved: true

  - name: 'p1-escalation'
    webhook_configs:
      - url: 'https://oapi.dingtalk.com/robot/send?access_token=P1_TOKEN'
        send_resolved: true
    email_configs:
      - to: 'oncall@company.com'
        send_resolved: true
    # 电话通知（PagerDuty / OpsGenie）
    pagerduty_configs:
      - service_key: 'REDACTED'
        severity: critical

  - name: 'sre-p1'
    webhook_configs:
      - url: 'https://oapi.dingtalk.com/robot/send?access_token=SRE_P1_TOKEN'

  - name: 'team-dingtalk'
    webhook_configs:
      - url: 'https://oapi.dingtalk.com/robot/send?access_token=TEAM_TOKEN'

  - name: 'app-team-dingtalk'
    webhook_configs:
      - url: 'https://oapi.dingtalk.com/robot/send?access_token=APP_TOKEN'

  - name: 'biz-team-dingtalk'
    webhook_configs:
      - url: 'https://oapi.dingtalk.com/robot/send?access_token=BIZ_TOKEN'

  - name: 'business-call'
    webhook_configs:
      - url: 'https://oapi.dingtalk.com/robot/send?access_token=BIZ_CALL_TOKEN'
    email_configs:
      - to: 'business-leaders@company.com'
```

### 6.2 静默规则示例

```bash
# 维护期间静默所有告警
amtool silence add \
    --comment "维护窗口 2024-04-20 02:00-04:00 升级数据库" \
    --duration 2h \
    --author "chen.dba" \
    match_type=regexp instance="mysql-.*"

# 静默特定服务
amtool silence add \
    --comment "order-service 灰度发布观察" \
    --duration 1h \
    --author "zhou.devops" \
    app="order-service"
```

## 7. 告警分级标准

| 级别 | 名称 | 定义 | 响应时间 | 通知方式 | 升级路径 |
| --- | --- | --- | --- | --- | --- |
| P1 | 紧急 | 核心业务中断、数据丢失风险 | 5 分钟 | 电话 + 钉钉 + 邮件 | 15min → 经理，30min → 总监 |
| P2 | 重要 | 部分功能不可用、性能严重劣化 | 15 分钟 | 钉钉 + 邮件 | 1h → 经理 |
| P3 | 一般 | 单实例异常、容量预警 | 1 小时 | 钉钉 | 工作时间处理 |
| P4 | 提醒 | 容量趋势、低风险异常 | 工作日 | 邮件 | 计划处理 |

## 8. Grafana Dashboard 配置

### 8.1 主机监控面板（变量设计）

```json
{
  "templating": {
    "list": [
      {
        "name": "datasource",
        "type": "datasource",
        "query": "prometheus",
        "current": { "text": "Thanos", "value": "Thanos" }
      },
      {
        "name": "instance",
        "type": "query",
        "datasource": "${datasource}",
        "query": "label_values(node_uname_info, instance)",
        "refresh": 1
      }
    ]
  }
}
```

### 8.2 核心 Panel PromQL 示例

```promql
# CPU 使用率（多核堆叠）
1 - avg by (mode, instance) (rate(node_cpu_seconds_total{instance=~"$instance"}[5m]))

# 内存使用率
1 - (node_memory_MemAvailable_bytes{instance=~"$instance"} / node_memory_MemTotal_bytes{instance=~"$instance"})

# 网络吞吐
rate(node_network_receive_bytes_total{instance=~"$instance",device!~"lo|veth.*"}[5m]) * 8

# 磁盘 IO
rate(node_disk_reads_completed_total{instance=~"$instance"}[5m])

# 应用 QPS（按状态码）
sum by (status) (rate(http_requests_total{app="$app"}[5m]))

# P50/P95/P99 延迟
histogram_quantile(0.50, sum by (app, le) (rate(http_request_duration_seconds_bucket{app="$app"}[5m])))
histogram_quantile(0.95, sum by (app, le) (rate(http_request_duration_seconds_bucket{app="$app"}[5m])))
histogram_quantile(0.99, sum by (app, le) (rate(http_request_duration_seconds_bucket{app="$app"}[5m])))
```

## 9. 告警通知渠道

### 9.1 钉钉机器人

```yaml
receivers:
  - name: 'team-dingtalk'
    webhook_configs:
      - url: 'https://oapi.dingtalk.com/robot/send?access_token=XXX'
        send_resolved: true
```

钉钉告警模板（Go template）：

```
{{ define "dingtalk.title" }}{{ .Status | toUpper }} {{ .CommonLabels.alertname }}{{ end }}
{{ define "dingtalk.content" }}
### 告警状态: {{ .Status | toUpper }}
- 告警名称: {{ .CommonLabels.alertname }}
- 严重级别: {{ .CommonLabels.severity }}
- 影响对象: {{ .CommonLabels.instance }}
- 触发时间: {{ .StartsAt.Format "2006-01-02 15:04:05" }}
{{ range .Alerts }}
- 详情: {{ .Annotations.summary }}
- 描述: {{ .Annotations.description }}
- Runbook: {{ .Annotations.runbook }}
{{ end }}
{{ end }}
```

### 9.2 企业微信

```yaml
- name: 'wecom'
  webhook_configs:
    - url: 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=XXX'
```

### 9.3 电话告警

通过 PagerDuty 或 OpsGenie 实现电话升级：

```yaml
- name: 'phone-escalation'
  pagerduty_configs:
    - service_key: 'REDACTED'
      severity: '{{ .CommonLabels.severity }}'
      description: '{{ .CommonAnnotations.summary }}'
```

## 10. 告警处理流程

### 10.1 标准处理流程（ALERT 模型）

```
A  Acknowledge    接收并确认告警（在钉钉群回复 "认领"）
L  Locate         根据 Runbook 链接定位问题
E  Evaluate       评估影响范围与紧急程度
R  Resolve        执行恢复操作
T  Ticket         严重事件创建工单 + 复盘
```

### 10.2 值班响应 SLA

| 级别 | 接收后认领 | 开始处理 | 升级 |
| --- | --- | --- | --- |
| P1 | 5 分钟 | 5 分钟 | 15min 未认领 → 备班 → 经理 |
| P2 | 15 分钟 | 15 分钟 | 1h 未处理 → 经理 |
| P3 | 1 小时 | 工作时间 | - |
| P4 | 工作日 | 工作日 | - |

### 10.3 复盘要求

P1/P2 事件需在 48 小时内完成复盘，包含：时间线、根因、影响、改进措施、责任人、完成期限。复盘模板见 `https://wiki.example.com/sre/postmortem`。

## 11. 变更记录

| 版本 | 日期 | 修改内容 | 修改人 |
| --- | --- | --- | --- |
| v1.0 | 2022-05-10 | 初版发布 | 刘洋 |
| v1.5 | 2023-02-20 | 增加 Thanos 长期存储 | 刘洋 |
| v2.0 | 2023-06-15 | 引入分级路由与抑制规则 | 王芳 |
| v2.3 | 2023-10-30 | 增加业务与黑盒告警 | 刘洋 |
| v2.5 | 2024-04-15 | 增加告警处理 SLA 与复盘流程 | 刘洋 |

## 12. 附录

### 12.1 常用 PromQL 速查

```promql
# TopN CPU 主机
topk(5, node:cpu_usage:ratio)

# 错误率排序
topk(5, http:error_rate5m)

# 容量预测：按当前增长速率预测 7 天后磁盘使用
predict_linear(node_filesystem_avail_bytes[6h], 7*24*3600) < 0

# 同环比
rate(order_created_total[10m]) / rate(order_created_total[10m] offset 1d)
```

### 12.2 调试命令

```bash
# 检查告警规则
amtool check-rules /etc/prometheus/rules/*.yml

# 查看当前告警
amtool alert --alertmanager.url=http://alertmanager:9093

# 测试告警路由
amtool config routes test --config.file=alertmanager.yml severity=critical team=sre

# Prometheus 查询
curl -G 'http://prometheus:9090/api/v1/query' --data-urlencode 'query=up'
```

### 12.3 参考资料

- Prometheus 官方文档：https://prometheus.io/docs/
- Alertmanager 配置：https://prometheus.io/docs/alerting/latest/configuration/
- 公司监控大盘：https://grafana.company.com
- Runbook 知识库：https://wiki.example.com/sre/runbook
