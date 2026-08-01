# MySQL 数据库备份与恢复 SOP

| 文档属性 | 内容 |
| --- | --- |
| 文档编号 | SOP-OPS-MYSQL-BACKUP-002 |
| 分类 | ops_sop |
| 版本 | v1.8 |
| 适用范围 | 公司所有 MySQL 5.7 / 8.0 主从架构下的数据库备份、恢复、验证操作 |
| 维护人 | DBA 组 - 陈晨 |
| 生效日期 | 2024-04-01 |
| 审核人 | 运维总监 李伟 |

---

## 1. 目的与范围

本 SOP 规范公司核心业务 MySQL 数据库的备份策略、备份执行、恢复演练、异地容灾和监控告警，确保在数据丢失、误删、硬件故障等场景下能够在 RTO（恢复时间目标）≤ 2 小时、RPO（恢复点目标）≤ 5 分钟内恢复业务。

适用版本：MySQL 5.7.x、MySQL 8.0.x；适用架构：单机、主从、MHA、MGR。

## 2. 备份策略

### 2.1 备份层次与频率

| 备份类型 | 频率 | 保留周期 | 数据量预估 | 工具 | RPO |
| --- | --- | --- | --- | --- | --- |
| 全量逻辑备份 | 每日 02:00 | 30 天 | ~80 GB | mysqldump | ≤ 24h |
| 全量物理备份 | 每周日 03:00 | 8 周 | ~120 GB | xtrabackup | ≤ 7d |
| 增量物理备份 | 每日 03:00（除周日） | 7 天 | ~5 GB | xtrabackup | ≤ 24h |
| Binlog 实时备份 | 实时 | 14 天 | - | mysqlbinlog | ≤ 5 min |
| 异地备份 | 每日 | 90 天 | - | rsync + OSS | - |

### 2.2 备份保留策略

- 短期保留：本地磁盘 7 天，便于快速恢复
- 中期保留：异地 NAS 30 天
- 长期归档：对象存储（OSS/S3）90 天， Glacier 模式冷存储 1 年
- 合规保留：财务相关数据按法规保留 7 年

## 3. 备份工具对比

| 工具 | 备份方式 | 速度 | 体积 | 是否锁表 | PITR 支持 | 适用场景 |
| --- | --- | --- | --- | --- | --- | --- |
| mysqldump | 逻辑备份 | 慢 | 中 | FTWRL 短暂 | 是（配 binlog） | 小库（<50GB）、跨版本迁移 |
| mysqlpump | 逻辑备份（并行） | 较快 | 中 | FTWRL 短暂 | 是 | 中库（50-200GB） |
| xtrabackup | 物理备份 | 快 | 大 | 不锁 InnoDB | 是 | 大库（>200GB）、生产首选 |
| mydumper | 逻辑备份（多线程） | 快 | 中 | FTWRL 短暂 | 是 | 大库逻辑备份 |
| mysqlbinlog | 增量 binlog | - | 小 | 无 | 是 | PITR 必备 |

生产环境推荐组合：**xtrabackup 周全量 + 日增量 + binlog 实时备份**。

## 4. my.cnf 关键配置参数

```ini
# /etc/my.cnf 关键配置

[mysqld]
# 基础配置
datadir                  = /data/mysql/data
socket                   = /data/mysql/mysql.sock
pid-file                 = /data/mysql/mysqld.pid
log-error                = /data/mysql/log/error.log
user                     = mysql

# 二进制日志（PITR 必备）
server-id                = 100
log_bin                  = /data/mysql/binlog/mysql-bin
binlog_format            = ROW
binlog_row_image         = MINIMAL
expire_logs_days         = 14
max_binlog_size          = 512M
sync_binlog              = 1

# GTID（推荐开启，便于 PITR 定位）
gtid_mode                = ON
enforce_gtid_consistency = ON
log_slave_updates        = ON

# InnoDB 配置
innodb_buffer_pool_size  = 32G
innodb_log_file_size     = 2G
innodb_log_buffer_size   = 64M
innodb_flush_log_at_trx_commit = 1
innodb_file_per_table    = 1
innodb_data_file_path    = ibdata1:1G:autoextend

# 备份相关
innodb_max_dirty_pages_pct = 75
innodb_max_dirty_pages_pct_lwm = 50
innodb_io_capacity       = 2000
innodb_io_capacity_max   = 4000

# 复制相关（从库）
relay_log                = /data/mysql/relay/relay-bin
relay_log_recovery       = ON
read_only                = ON
super_read_only          = ON

[client]
default-character-set    = utf8mb4

[mysqldump]
max_allowed_packet       = 512M
single-transaction       = 1
```

参数说明：

| 参数 | 说明 |
| --- | --- |
| `binlog_format=ROW` | 行级 binlog，PITR 精度最高 |
| `sync_binlog=1` | 每次提交都刷盘，最强一致性 |
| `expire_logs_days=14` | binlog 保留 14 天，覆盖备份周期 |
| `gtid_mode=ON` | 启用 GTID，简化 PITR 定位 |
| `innodb_buffer_pool_size` | 物理内存 60%-70% |

## 5. 全量备份操作步骤

### 5.1 mysqldump 逻辑全量备份

```bash
#!/bin/bash
# /opt/scripts/mysql_full_dump.sh

set -euo pipefail

BACKUP_DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR=/data/backup/mysql/dump
BACKUP_FILE=${BACKUP_DIR}/full_${BACKUP_DATE}.sql.gz
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=backup
MYSQL_PASSWORD='Backup@2024!secure'
RETENTION_DAYS=7
NOTIFY_WEBHOOK="https://oapi.dingtalk.com/robot/send?access_token=xxxxx"

mkdir -p ${BACKUP_DIR}

log() { echo "[$(date '+%F %T')] $1"; }

notify() {
    curl -s -X POST "${NOTIFY_WEBHOOK}" \
        -H 'Content-Type: application/json' \
        -d "{\"msgtype\":\"text\",\"text\":{\"content\":\"MySQL 备份通知: $1\"}}"
}

trap 'notify "❌ 全量备份失败: ${BACKUP_FILE}"; exit 1' ERR

log "开始全量逻辑备份: ${BACKUP_FILE}"

mysqldump \
    --host=${MYSQL_HOST} \
    --port=${MYSQL_PORT} \
    --user=${MYSQL_USER} \
    --password="${MYSQL_PASSWORD}" \
    --single-transaction \
    --master-data=2 \
    --default-character-set=utf8mb4 \
    --routines \
    --triggers \
    --events \
    --set-gtid-purged=OFF \
    --hex-blob \
    --quick \
    --all-databases \
    | gzip > ${BACKUP_FILE}

# 校验文件完整性
if [ ! -s ${BACKUP_FILE} ]; then
    log "备份文件为空，异常退出"
    notify "❌ 备份文件为空: ${BACKUP_FILE}"
    exit 1
fi

# 计算校验和
md5sum ${BACKUP_FILE} > ${BACKUP_FILE}.md5
sha256sum ${BACKUP_FILE} > ${BACKUP_FILE}.sha256

# 文件大小
FILE_SIZE=$(du -h ${BACKUP_FILE} | awk '{print $1}')
log "备份完成，文件大小: ${FILE_SIZE}"

# 清理过期备份
find ${BACKUP_DIR} -name "full_*.sql.gz*" -mtime +${RETENTION_DAYS} -delete
log "清理 ${RETENTION_DAYS} 天前的备份"

# 上传至异地 OSS
ossutil cp ${BACKUP_FILE} oss://company-mysql-backup/dump/$(date +%Y%m%d)/ --config-file=/etc/ossutil.conf
ossutil cp ${BACKUP_FILE}.md5 oss://company-mysql-backup/dump/$(date +%Y%m%d)/ --config-file=/etc/ossutil.conf

log "异地备份上传完成"
notify "✅ 全量备份成功: ${BACKUP_FILE} (大小: ${FILE_SIZE})"

# 加入 cron: 0 2 * * * /opt/scripts/mysql_full_dump.sh >> /var/log/mysql_backup.log 2>&1
```

参数说明：

| 参数 | 说明 |
| --- | --- |
| `--single-transaction` | InnoDB 一致性快照，不锁表 |
| `--master-data=2` | 记录 binlog 位置（注释行），便于 PITR |
| `--routines` | 备份存储过程和函数 |
| `--triggers` | 备份触发器 |
| `--events` | 备份定时事件 |
| `--hex-blob` | 二进制字段以十六进制存储 |
| `--quick` | 大表流式输出，不缓存到内存 |

### 5.2 xtrabackup 物理全量备份

```bash
#!/bin/bash
# /opt/scripts/mysql_xtrabackup_full.sh

set -euo pipefail

BACKUP_DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_ROOT=/data/backup/mysql/xtrabackup
BACKUP_DIR=${BACKUP_ROOT}/full_${BACKUP_DATE}
MYSQL_USER=backup
MYSQL_PASSWORD='Backup@2024!secure'
RETENTION_WEEKS=8

mkdir -p ${BACKUP_ROOT}

# 1. 创建全量备份
xtrabackup --backup \
    --user=${MYSQL_USER} \
    --password=${MYSQL_PASSWORD} \
    --target-dir=${BACKUP_DIR} \
    --parallel=4 \
    --compress \
    --compress-threads=4 \
    --no-lock

# 2. prepare 阶段（应用 redo log，使备份一致性可恢复）
xtrabackup --prepare \
    --target-dir=${BACKUP_DIR} \
    --parallel=4

# 3. 记录 binlog 位点（用于后续 PITR）
cat ${BACKUP_DIR}/xtrabackup_binlog_info
cat ${BACKUP_DIR}/xtrabackup_gtid_info

# 4. 打包
tar -czf ${BACKUP_DIR}.tar.gz -C ${BACKUP_ROOT} full_${BACKUP_DATE}
rm -rf ${BACKUP_DIR}

# 5. 清理 8 周前的全量备份
find ${BACKUP_ROOT} -name "full_*.tar.gz" -mtime +$((RETENTION_WEEKS*7)) -delete

# 6. 上传 OSS
ossutil cp ${BACKUP_DIR}.tar.gz oss://company-mysql-backup/xtrabackup/$(date +%Y%m%d)/ --config-file=/etc/ossutil.conf
```

## 6. 增量备份操作步骤

### 6.1 xtrabackup 增量备份

```bash
#!/bin/bash
# /opt/scripts/mysql_xtrabackup_incr.sh

set -euo pipefail

BACKUP_DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_ROOT=/data/backup/mysql/xtrabackup
INCR_DIR=${BACKUP_ROOT}/incr_${BACKUP_DATE}
BASE_DIR=$(ls -dt ${BACKUP_ROOT}/full_* 2>/dev/null | head -1)

if [ -z "${BASE_DIR}" ]; then
    echo "未找到全量备份基线，请先执行全量备份"
    exit 1
fi

# 增量备份基于最近的全量备份
xtrabackup --backup \
    --user=backup \
    --password='Backup@2024!secure' \
    --target-dir=${INCR_DIR} \
    --incremental-basedir=${BASE_DIR} \
    --parallel=4 \
    --compress \
    --compress-threads=4

echo "增量备份完成: ${INCR_DIR}"
cat ${INCR_DIR}/xtrabackup_binlog_info

# 7 天后清理
find ${BACKUP_ROOT} -name "incr_*" -mtime +7 -delete
```

### 6.2 Binlog 实时备份

在备份机上配置 binlog 实时拉取：

```bash
# mysqlbinlog 实时拉取 binlog
mysqlbinlog \
    --read-from-remote-server \
    --raw \
    --host=10.0.1.21 \
    --user=repl \
    --password='Repl@2024!secure' \
    --stop-never \
    --stop-never-slave-server-id=999 \
    --result-file=/data/backup/mysql/binlog/ \
    mysql-bin.001234
```

或使用 `mysqlbinlog --read-from-remote-server --stop-never` 持续拉取。配合 systemd 管理：

```ini
# /etc/systemd/system/mysql-binlog-backup.service
[Unit]
Description=MySQL Binlog Remote Backup
After=network.target

[Service]
Type=simple
ExecStart=/opt/scripts/mysql_binlog_backup.sh
Restart=always
RestartSec=10
User=mysql

[Install]
WantedBy=multi-user.target
```

## 7. 恢复操作步骤

### 7.1 全量恢复（mysqldump）

```bash
# 1. 解压备份文件
gunzip -c /data/backup/mysql/dump/full_20240415_020000.sql.gz > /tmp/restore.sql

# 2. 在新实例上恢复（建议先停止应用连接）
mysql -uroot -p < /tmp/restore.sql

# 3. 验证数据
mysql -uroot -p -e "
    SELECT COUNT(*) FROM orders.orders;
    SELECT COUNT(*) FROM users.users;
    SHOW MASTER STATUS;
"
```

### 7.2 xtrabackup 物理恢复

```bash
# 1. 停止 MySQL
systemctl stop mysqld

# 2. 清空数据目录（务必确认！）
mv /data/mysql/data /data/mysql/data_$(date +%Y%m%d)_bak
mkdir -p /data/mysql/data

# 3. 解压并 prepare 全量备份
mkdir -p /tmp/restore
tar -xzf /data/backup/mysql/xtrabackup/full_20240414.tar.gz -C /tmp/restore
xtrabackup --prepare --target-dir=/tmp/restore/full_20240414

# 4. 如有增量，依次 apply 增量
xtrabackup --prepare --target-dir=/tmp/restore/full_20240414 \
    --incremental-dir=/tmp/restore/incr_20240415

# 5. 拷贝数据
xtrabackup --copy-back --target-dir=/tmp/restore/full_20240414

# 6. 修正权限
chown -R mysql:mysql /data/mysql/data

# 7. 启动 MySQL
systemctl start mysqld

# 8. 验证
mysql -uroot -p -e "SHOW DATABASES;"
```

### 7.3 基于 PITR（时间点恢复）

**场景**：2024-04-15 14:30:00 误删了 orders 表，需要恢复到 14:29:59。

```bash
# 1. 从最近的全量备份恢复（参见 7.1 或 7.2）
# 假设全量备份位点：mysql-bin.001234, position=12345678

# 2. 拉取该位点之后的 binlog
mysqlbinlog \
    --start-position=12345678 \
    --stop-datetime="2024-04-15 14:29:59" \
    /data/backup/mysql/binlog/mysql-bin.001234 \
    /data/backup/mysql/binlog/mysql-bin.001235 \
    /data/backup/mysql/binlog/mysql-bin.001236 \
    > /tmp/pitr.sql

# 3. 检查 pitr.sql 中的危险语句（DROP/DELETE/TRUNCATE）
grep -iE "drop|delete|truncate" /tmp/pitr.sql

# 4. 应用 binlog 恢复
mysql -uroot -p < /tmp/pitr.sql

# 5. 验证
mysql -uroot -p -e "SELECT COUNT(*) FROM orders.orders WHERE create_time < '2024-04-15 14:30:00';"
```

基于 GTID 的 PITR：

```bash
# 找到全量备份时的 GTID
cat /data/backup/mysql/xtrabackup/full_20240414/xtrabackup_gtid_info
# 输出示例：3E11FA47-71CA-11E1-9E33-C80AA9429562:1-12345

# 重放该 GTID 之后的 binlog
mysqlbinlog \
    --skip-gtids=true \
    --include-gtids='3E11FA47-71CA-11E1-9E33-C80AA9429562:12346-13000' \
    /data/backup/mysql/binlog/mysql-bin.001234 \
    | mysql -uroot -p
```

### 7.4 数据一致性验证 SQL

```sql
-- 1. 表行数核对
SELECT table_schema, table_name, table_rows
FROM information_schema.tables
WHERE table_schema IN ('orders','users','products')
ORDER BY table_schema, table_name;

-- 2. 关键表 checksum
CHECKSUM TABLE orders.orders EXTENDED;
CHECKSUM TABLE users.users EXTENDED;

-- 3. 与从库对比 binlog 位点
SHOW MASTER STATUS;

-- 4. 验证关键业务数据
SELECT
    DATE(create_time) AS dt,
    COUNT(*) AS order_cnt,
    SUM(amount) AS total_amount
FROM orders.orders
WHERE create_time >= '2024-04-14'
GROUP BY DATE(create_time);

-- 5. GTID 一致性
SELECT @@global.gtid_executed;
```

## 8. 备份验证

### 8.1 定期恢复测试流程

每月执行一次完整恢复演练：

```bash
#!/bin/bash
# /opt/scripts/mysql_backup_verify.sh

set -euo pipefail

TEST_PORT=3316
TEST_DIR=/data/mysql_test
BACKUP_FILE=$(ls -t /data/backup/mysql/dump/full_*.sql.gz | head -1)

echo "[1/6] 启动测试实例"
mysqld --initialize-insecure --datadir=${TEST_DIR} --user=mysql
mysqld_safe --datadir=${TEST_DIR} --port=${TEST_PORT} --socket=/tmp/mysql_test.sock --user=mysql &
sleep 10

echo "[2/6] 恢复备份"
gunzip -c ${BACKUP_FILE} | mysql --socket=/tmp/mysql_test.sock -uroot

echo "[3/6] 验证表数量"
PROD_TABLES=$(mysql -h10.0.1.21 -ubackup -p'Backup@2024!secure' -N -e "
    SELECT COUNT(*) FROM information_schema.tables WHERE table_schema NOT IN ('information_schema','performance_schema','mysql','sys')")
TEST_TABLES=$(mysql --socket=/tmp/mysql_test.sock -uroot -N -e "
    SELECT COUNT(*) FROM information_schema.tables WHERE table_schema NOT IN ('information_schema','performance_schema','mysql','sys')")

if [ "${PROD_TABLES}" != "${TEST_TABLES}" ]; then
    echo "❌ 表数量不一致: 生产=${PROD_TABLES}, 测试=${TEST_TABLES}"
    exit 1
fi

echo "[4/6] 验证关键表行数（抽样）"
for TABLE in "orders.orders" "users.users" "products.products"; do
    PROD_CNT=$(mysql -h10.0.1.21 -ubackup -p'Backup@2024!secure' -N -e "SELECT COUNT(*) FROM ${TABLE}")
    TEST_CNT=$(mysql --socket=/tmp/mysql_test.sock -uroot -N -e "SELECT COUNT(*) FROM ${TABLE}")
    if [ "${PROD_CNT}" != "${TEST_CNT}" ]; then
        echo "❌ ${TABLE} 行数不一致: 生产=${PROD_CNT}, 测试=${TEST_CNT}"
        exit 1
    fi
done

echo "[5/6] checksum 校验"
for TABLE in "orders.orders" "users.users"; do
    PROD_CS=$(mysql -h10.0.1.21 -ubackup -p'Backup@2024!secure' -N -e "CHECKSUM TABLE ${TABLE} EXTENDED" | awk '{print $2}')
    TEST_CS=$(mysql --socket=/tmp/mysql_test.sock -uroot -N -e "CHECKSUM TABLE ${TABLE} EXTENDED" | awk '{print $2}')
    if [ "${PROD_CS}" != "${TEST_CS}" ]; then
        echo "⚠️ ${TABLE} checksum 不一致（可能因备份期间业务写入）"
    fi
done

echo "[6/6] 清理测试实例"
mysqladmin --socket=/tmp/mysql_test.sock -uroot shutdown
rm -rf ${TEST_DIR}

echo "✅ 备份恢复验证通过"
```

## 9. 备份监控

### 9.1 监控项与告警阈值

| 监控项 | 检查方式 | 告警阈值 | 级别 |
| --- | --- | --- | --- |
| 备份任务是否执行 | cron 日志 | 连续 1 次未执行 | P2 |
| 备份文件大小 | `du -sh` | 较昨日下降 > 30% | P2 |
| 备份文件是否为空 | `-s` 判断 | 文件大小 = 0 | P1 |
| 备份耗时 | 脚本计时 | > 4 小时 | P3 |
| binlog 推送延迟 | `SHOW MASTER STATUS` | > 60 秒 | P2 |
| 磁盘剩余空间 | `df` | < 20% | P1 |
| 异地备份同步状态 | OSS API | 连续 1 次失败 | P2 |
| 恢复演练成功率 | 月度统计 | < 100% | P1 |

### 9.2 监控脚本示例

```bash
#!/bin/bash
# /opt/scripts/mysql_backup_monitor.sh

BACKUP_DIR=/data/backup/mysql/dump
TODAY=$(date +%Y%m%d)
LATEST_FILE=$(ls -t ${BACKUP_DIR}/full_*.sql.gz 2>/dev/null | head -1)

# 1. 检查今日是否已备份
if [[ "${LATEST_FILE}" != *"${TODAY}"* ]]; then
    echo "CRITICAL: 今日尚未生成全量备份"
    exit 2
fi

# 2. 检查文件大小
FILE_SIZE=$(stat -c %s ${LATEST_FILE})
if [ ${FILE_SIZE} -lt 1048576 ]; then
    echo "CRITICAL: 备份文件小于 1MB: ${LATEST_FILE}"
    exit 2
fi

# 3. 与昨日对比
YESTERDAY_FILE=$(ls -t ${BACKUP_DIR}/full_*.sql.gz 2>/dev/null | sed -n '2p')
if [ -n "${YESTERDAY_FILE}" ]; then
    YESTERDAY_SIZE=$(stat -c %s ${YESTERDAY_FILE})
    RATIO=$(echo "scale=2; (${FILE_SIZE} - ${YESTERDAY_SIZE}) / ${YESTERDAY_SIZE} * 100" | bc)
    if (( $(echo "${RATIO} < -30" | bc -l) )); then
        echo "WARNING: 备份文件较昨日下降 ${RATIO}%"
        exit 1
    fi
fi

# 4. 磁盘空间
DISK_USAGE=$(df /data/backup | tail -1 | awk '{print $5}' | tr -d '%')
if [ ${DISK_USAGE} -gt 80 ]; then
    echo "CRITICAL: 备份磁盘使用率 ${DISK_USAGE}%"
    exit 2
fi

echo "OK: 备份正常, 文件大小 $(numfmt --to=iec ${FILE_SIZE})"
exit 0
```

## 10. 异地备份

### 10.1 rsync 同步到异地 NAS

```bash
#!/bin/bash
# /opt/scripts/mysql_backup_rsync.sh

set -euo pipefail

BACKUP_DIR=/data/backup/mysql
REMOTE_NAS=nas@10.0.99.100::mysql_backup/$(date +%Y%m%d)
RSYNC_PASSWORD_FILE=/etc/rsync.password

# 使用 rsync 增量同步
rsync -avz --delete \
    --password-file=${RSYNC_PASSWORD_FILE} \
    --bwlimit=51200 \
    ${BACKUP_DIR}/ ${REMOTE_NAS}/

# 保留异地 30 天
ssh nas@10.0.99.100 "find /data/mysql_backup -maxdepth 1 -type d -mtime +30 -exec rm -rf {} \;"
```

### 10.2 上传对象存储（OSS/S3）

```bash
# 配置 ossutil
cat > /etc/ossutil.conf <<EOF
[Credentials]
provider=oss
accessKeyID=LTAI5tXXXXXXXX
accessKeySecret=XXXXXXXXXXXXXXXX
endpoint=oss-cn-beijing-internal.aliyuncs.com
EOF

# 上传并设置生命周期
ossutil cp /data/backup/mysql/dump/full_20240415_020000.sql.gz \
    oss://company-mysql-backup/dump/$(date +%Y%m%d)/ \
    --config-file=/etc/ossutil.conf \
    --meta "x-oss-object-acl:private" \
    --tag "env=prod,db=mysql"

# 90 天后自动转 IA，180 天后转 Archive
# 在 OSS 控制台配置生命周期规则
```

## 11. 应急响应流程

1. **故障发现**：监控告警 / 业务报错 / DBA 巡检
2. **影响评估**：确认丢失范围、影响业务线、数据时间窗口
3. **方案决策**：根据 RTO/RPO 选择全量恢复或 PITR
4. **执行恢复**：按本 SOP 第 7 章操作，双人复核
5. **数据校验**：执行第 7.4 节一致性验证 SQL
6. **业务恢复**：应用连接切换，灰度放量
7. **复盘总结**：48 小时内输出事故报告，更新本 SOP

## 12. 变更记录

| 版本 | 日期 | 修改内容 | 修改人 |
| --- | --- | --- | --- |
| v1.0 | 2022-03-10 | 初版发布 | 陈晨 |
| v1.3 | 2023-05-20 | 增加 GTID PITR 流程 | 陈晨 |
| v1.5 | 2023-09-15 | 增加 xtrabackup 增量备份 | 林涛 |
| v1.7 | 2024-01-08 | 增加异地 OSS 备份 | 陈晨 |
| v1.8 | 2024-04-01 | 完善监控告警阈值 | 陈晨 |

## 13. 附录

### 13.1 备份目录结构

```
/data/backup/mysql/
├── dump/                              # mysqldump 全量
│   ├── full_20240415_020000.sql.gz
│   └── full_20240415_020000.sql.gz.md5
├── xtrabackup/                        # xtrabackup 物理备份
│   ├── full_20240414_030000.tar.gz
│   └── incr_20240415_030000/
├── binlog/                            # binlog 远程备份
│   ├── mysql-bin.001234
│   └── mysql-bin.001235
└── verify/                            # 恢复测试目录
    └── mysql_test/
```

### 13.2 备份账号权限

```sql
CREATE USER 'backup'@'127.0.0.1' IDENTIFIED BY 'Backup@2024!secure';
GRANT SELECT, PROCESS, RELOAD, LOCK TABLES, REPLICATION CLIENT, SHOW VIEW, EVENT, TRIGGER ON *.* TO 'backup'@'127.0.0.1';
ALTER USER 'backup'@'127.0.0.1' REQUIRE SSL;
```

### 13.3 参考资料

- Percona XtraBackup 文档：https://docs.percona.com/percona-xtrabackup/
- MySQL 官方备份恢复：https://dev.mysql.com/doc/refman/8.0/en/backup-and-recovery.html
- 公司 DBA 知识库：https://wiki.example.com/dba
