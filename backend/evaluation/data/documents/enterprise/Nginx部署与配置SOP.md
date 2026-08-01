# Nginx 部署与配置 SOP

| 文档属性 | 内容 |
| --- | --- |
| 文档编号 | SOP-OPS-NGINX-001 |
| 分类 | ops_sop |
| 版本 | v2.3 |
| 适用范围 | 公司所有基于 Nginx 的反向代理、静态资源、负载均衡部署场景 |
| 维护人 | 基础架构组 - 张磊 |
| 生效日期 | 2024-03-15 |
| 审核人 | 运维总监 李伟 |

---

## 1. 目的与适用范围

本 SOP 用于规范公司内部 Nginx 反向代理服务器的部署、配置、安全加固、性能调优及日常运维操作，确保各业务线 Nginx 实例配置统一、可追溯、可快速恢复。适用于开发、测试、预发、生产环境的 Nginx 1.20+ 版本部署。

## 2. 部署环境准备

### 2.1 系统要求

| 项目 | 最低要求 | 推荐配置（生产） |
| --- | --- | --- |
| 操作系统 | CentOS 7.9 / Ubuntu 20.04 | CentOS 8.5 / Rocky Linux 9 |
| 内核版本 | 3.10+ | 5.4+ |
| CPU | 2 核 | 8 核以上（高并发场景） |
| 内存 | 2 GB | 16 GB |
| 磁盘 | 20 GB | 100 GB SSD（日志单独挂载） |
| 文件描述符 | 65535 | 1048576 |

### 2.2 依赖安装

CentOS / Rocky Linux：

```bash
# 安装编译工具链和依赖
yum groupinstall -y "Development Tools"
yum install -y gcc gcc-c++ make automake autoconf libtool \
    pcre pcre-devel zlib zlib-devel openssl openssl-devel \
    libxml2 libxml2-devel libxslt libxslt-devel \
    gd gd-devel GeoIP GeoIP-devel \
    wget curl vim

# 调整文件描述符上限
echo "* soft nofile 1048576" >> /etc/security/limits.conf
echo "* hard nofile 1048576" >> /etc/security/limits.conf

# 内核参数优化
cat >> /etc/sysctl.conf <<EOF
net.ipv4.tcp_max_syn_backlog = 65535
net.ipv4.tcp_syncookies = 1
net.ipv4.tcp_tw_reuse = 1
net.ipv4.tcp_fin_timeout = 30
net.core.somaxconn = 65535
net.core.netdev_max_backlog = 65535
EOF
sysctl -p
```

Ubuntu / Debian：

```bash
apt update
apt install -y build-essential libpcre3 libpcre3-dev zlib1g-dev \
    libssl-dev libxml2-dev libxslt1-dev libgd-dev libgeoip-dev \
    wget curl vim
```

## 3. 安装步骤

### 3.1 方式一：源码编译安装（推荐生产环境）

源码编译便于自定义模块，可剔除不需要的功能降低攻击面。

```bash
# 创建运行用户
groupadd -r nginx
useradd -r -g nginx -s /sbin/nologin -d /var/cache/nginx nginx
mkdir -p /var/cache/nginx/client_temp /var/cache/nginx/proxy_temp
chown -R nginx:nginx /var/cache/nginx

# 下载源码（以 1.24.0 为例）
cd /usr/local/src
wget http://nginx.org/download/nginx-1.24.0.tar.gz
tar -zxvf nginx-1.24.0.tar.gz
cd nginx-1.24.0

# 编译配置
./configure \
    --prefix=/usr/local/nginx \
    --sbin-path=/usr/sbin/nginx \
    --conf-path=/etc/nginx/nginx.conf \
    --error-log-path=/var/log/nginx/error.log \
    --http-log-path=/var/log/nginx/access.log \
    --pid-path=/var/run/nginx.pid \
    --lock-path=/var/run/nginx.lock \
    --http-client-body-temp-path=/var/cache/nginx/client_temp \
    --http-proxy-temp-path=/var/cache/nginx/proxy_temp \
    --http-fastcgi-temp-path=/var/cache/nginx/fastcgi_temp \
    --http-uwsgi-temp-path=/var/cache/nginx/uwsgi_temp \
    --http-scgi-temp-path=/var/cache/nginx/scgi_temp \
    --user=nginx \
    --group=nginx \
    --with-compat \
    --with-file-aio \
    --with-threads \
    --with-http_ssl_module \
    --with-http_v2_module \
    --with-http_realip_module \
    --with-http_addition_module \
    --with-http_xslt_module=dynamic \
    --with-http_image_filter_module=dynamic \
    --with-http_geoip_module=dynamic \
    --with-http_sub_module \
    --with-http_dav_module \
    --with-http_flv_module \
    --with-http_mp4_module \
    --with-http_gunzip_module \
    --with-http_gzip_static_module \
    --with-http_random_index_module \
    --with-http_secure_link_module \
    --with-http_degradation_module \
    --with-http_slice_module \
    --with-http_stub_status_module \
    --with-mail=dynamic \
    --with-mail_ssl_module \
    --with-stream=dynamic \
    --with-stream_ssl_module \
    --with-stream_realip_module \
    --with-stream_ssl_preread_module \
    --with-pcre-jit \
    --with-cc-opt='-O2 -g -pipe -Wall -Wp,-D_FORTIFY_SOURCE=2 -fexceptions -fstack-protector-strong'

# 编译并安装
make -j $(nproc) && make install

# 注册 systemd 服务
cat > /usr/lib/systemd/system/nginx.service <<'EOF'
[Unit]
Description=The NGINX HTTP and reverse proxy server
After=syslog.target network.target remote-fs.target nss-lookup.target

[Service]
Type=forking
PIDFile=/var/run/nginx.pid
ExecStartPre=/usr/sbin/nginx -t
ExecStart=/usr/sbin/nginx
ExecReload=/usr/sbin/nginx -s reload
ExecStop=/bin/kill -s QUIT $MAINPID
PrivateTmp=true
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable nginx
```

### 3.2 方式二：yum 安装（适用于快速部署）

```bash
# 添加官方 yum 源
cat > /etc/yum.repos.d/nginx.repo <<'EOF'
[nginx-stable]
name=nginx stable repo
baseurl=http://nginx.org/packages/centos/$releasever/$basearch/
gpgcheck=1
enabled=1
gpgkey=https://nginx.org/keys/nginx_signing.key
module_hotfixes=true
EOF

yum install -y nginx
systemctl enable --now nginx
```

## 4. 核心配置说明

### 4.1 完整 nginx.conf 示例

```nginx
# /etc/nginx/nginx.conf
user  nginx;
worker_processes auto;
worker_cpu_affinity auto;
worker_rlimit_nofile 1048576;

error_log  /var/log/nginx/error.log warn;
pid        /var/run/nginx.pid;

events {
    worker_connections  65535;
    use epoll;
    multi_accept on;
}

http {
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;

    # 日志格式：结合 ELK 分析
    log_format  main  '$remote_addr - $remote_user [$time_local] "$request" '
                      '$status $body_bytes_sent "$http_referer" '
                      '"$http_user_agent" "$http_x_forwarded_for" '
                      'rt=$request_time urt=$upstream_response_time '
                      'uct=$upstream_connect_time uht=$upstream_header_time';

    access_log  /var/log/nginx/access.log  main buffer=32k flush=5s;

    sendfile        on;
    tcp_nopush      on;
    tcp_nodelay     on;
    types_hash_max_size 2048;
    server_tokens   off;             # 隐藏版本号

    keepalive_timeout   65;
    keepalive_requests  1000;

    client_max_body_size    50m;     # 限制请求体大小
    client_body_buffer_size 512k;
    client_body_timeout     60s;
    client_header_timeout   60s;
    send_timeout            60s;

    # Gzip 压缩
    gzip on;
    gzip_min_length 1k;
    gzip_comp_level 6;
    gzip_types text/plain text/css text/xml text/javascript
               application/javascript application/json application/xml
               application/rss+xml application/atom+xml image/svg+xml;
    gzip_vary on;
    gzip_disable "MSIE [1-6]\.";

    # 限流区域
    limit_req_zone $binary_remote_addr zone=api_limit:10m rate=100r/s;
    limit_req_zone $binary_remote_addr zone=login_limit:10m rate=5r/s;
    limit_conn_zone $binary_remote_addr zone=conn_limit:10m;

    # 上游服务 - API 服务集群
    upstream api_backend {
        least_conn;
        server 10.0.1.11:8080 max_fails=3 fail_timeout=30s;
        server 10.0.1.12:8080 max_fails=3 fail_timeout=30s;
        server 10.0.1.13:8080 max_fails=3 fail_timeout=30s backup;
        keepalive 32;
    }

    # 上游服务 - Web 前端静态资源
    upstream web_backend {
        ip_hash;
        server 10.0.2.11:80 max_fails=3 fail_timeout=30s;
        server 10.0.2.12:80 max_fails=3 fail_timeout=30s;
        keepalive 64;
    }

    # 代理缓存配置
    proxy_cache_path /var/cache/nginx/proxy_cache levels=1:2
                     keys_zone=api_cache:100m max_size=10g
                     inactive=60m use_temp_path=off;

    # SSL 优化参数
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:SSL:50m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;

    include /etc/nginx/conf.d/*.conf;
}
```

### 4.2 关键参数说明

| 参数 | 说明 | 推荐值 |
| --- | --- | --- |
| `worker_processes` | worker 进程数，建议等于 CPU 核数 | `auto` |
| `worker_connections` | 单个 worker 最大连接数 | 65535 |
| `keepalive_timeout` | 客户端 keep-alive 超时 | 65s |
| `keepalive_requests` | 单连接最大请求数 | 1000 |
| `client_max_body_size` | 请求体最大尺寸（防 413） | 50m |
| `proxy_connect_timeout` | 与后端建连超时 | 5s |
| `proxy_read_timeout` | 读后端响应超时 | 60s |
| `proxy_send_timeout` | 向后端发送超时 | 60s |
| `proxy_buffer_size` | 响应首部缓冲 | 16k |
| `proxy_buffers` | 响应内容缓冲 | 8 32k |

## 5. 反向代理配置示例

### 5.1 HTTP 反向代理（含 /api 和 / 路由）

```nginx
# /etc/nginx/conf.d/app.example.com.conf

# HTTP 跳转 HTTPS
server {
    listen 80;
    server_name app.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name app.example.com;

    ssl_certificate     /etc/nginx/ssl/app.example.com.crt;
    ssl_certificate_key /etc/nginx/ssl/app.example.com.key;

    # 安全响应头
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    # 禁用不必要 HTTP 方法
    if ($request_method !~ ^(GET|POST|PUT|DELETE|HEAD|OPTIONS|PATCH)$ ) {
        return 405;
    }

    # 健康检查端点（不记日志）
    location = /health {
        access_log off;
        return 200 "ok\n";
        add_header Content-Type text/plain;
    }

    # /api 反向代理到后端 API 服务
    location /api/ {
        limit_req zone=api_limit burst=200 nodelay;
        limit_conn conn_limit 50;

        proxy_pass http://api_backend;
        proxy_http_version 1.1;
        proxy_set_header Connection "";

        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_connect_timeout 5s;
        proxy_send_timeout    60s;
        proxy_read_timeout    60s;

        proxy_buffering on;
        proxy_buffer_size 16k;
        proxy_buffers 8 32k;
        proxy_busy_buffers_size 64k;

        proxy_next_upstream error timeout http_502 http_503 http_504;
        proxy_next_upstream_tries 2;
    }

    # 登录接口单独限流
    location /api/auth/login {
        limit_req zone=login_limit burst=10 nodelay;
        proxy_pass http://api_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    # 静态资源 - 浏览器长缓存
    location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|woff|woff2|ttf|eot)$ {
        proxy_pass http://web_backend;
        proxy_cache api_cache;
        proxy_cache_valid 200 304 12h;
        proxy_cache_key $scheme$proxy_host$request_uri;
        add_header X-Cache-Status $upstream_cache_status;
        expires 30d;
        add_header Cache-Control "public, immutable";
        access_log off;
    }

    # 根路径 - 前端单页应用
    location / {
        proxy_pass http://web_backend;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

## 6. HTTPS 配置

### 6.1 Let's Encrypt 证书申请

```bash
# 安装 certbot
yum install -y epel-release
yum install -y certbot python3-certbot-nginx

# 方式一：webroot 方式（不中断服务）
certbot certonly --webroot -w /var/www/html -d app.example.com -d www.example.com \
    --email ops@example.com --agree-tos --no-eff-email

# 方式二：standalone 方式（需暂停 80 端口）
nginx -s stop
certbot certonly --standalone -d app.example.com \
    --email ops@example.com --agree-tos --no-eff-email
nginx

# 证书路径
# 证书：/etc/letsencrypt/live/app.example.com/fullchain.pem
# 私钥：/etc/letsencrypt/live/app.example.com/privkey.pem
```

### 6.2 自动续期

```bash
# 测试续期
certbot renew --dry-run

# 配置 cron 自动续期（每天凌晨 3 点检查）
cat > /etc/cron.d/certbot-renew <<'EOF'
0 3 * * * root /usr/bin/certbot renew --quiet --post-hook "systemctl reload nginx"
EOF

# 或使用 systemd timer
cat > /etc/systemd/system/certbot-renew.service <<'EOF'
[Unit]
Description=Certbot Renew

[Service]
Type=oneshot
ExecStart=/usr/bin/certbot renew --quiet --post-hook "systemctl reload nginx"
EOF

cat > /etc/systemd/system/certbot-renew.timer <<'EOF'
[Unit]
Description=Daily Certbot Renewal

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl enable --now certbot-renew.timer
```

## 7. 性能调优参数

### 7.1 keepalive 优化

```nginx
http {
    keepalive_timeout   65;
    keepalive_requests  1000;
}

upstream api_backend {
    server 10.0.1.11:8080;
    keepalive 32;           # 与后端保持 32 个长连接
}

location /api/ {
    proxy_http_version 1.1;
    proxy_set_header Connection "";   # 清空 Connection 头启用长连接
}
```

### 7.2 Buffer 调优

```nginx
http {
    client_body_buffer_size   512k;
    client_header_buffer_size 4k;
    large_client_header_buffers 4 16k;

    proxy_buffer_size   16k;
    proxy_buffers       8 32k;
    proxy_busy_buffers_size 64k;
}
```

### 7.3 Timeout 调优

```nginx
http {
    client_body_timeout     60s;
    client_header_timeout   60s;
    send_timeout            60s;

    # 后端代理超时
    proxy_connect_timeout   5s;     # 建连超时，快速失败转移
    proxy_send_timeout      60s;
    proxy_read_timeout      60s;
}
```

## 8. 日志配置

### 8.1 access_log 配置

```nginx
log_format main '$remote_addr - $remote_user [$time_local] "$request" '
                '$status $body_bytes_sent "$http_referer" '
                '"$http_user_agent" "$http_x_forwarded_for" '
                'rt=$request_time urt=$upstream_response_time';

# 写入文件，缓冲 32KB，5 秒刷新
access_log /var/log/nginx/access.log main buffer=32k flush=5s;

# 排除健康检查
location = /health {
    access_log off;
    return 200 "ok\n";
}
```

### 8.2 logrotate 日志切割

```bash
# /etc/logrotate.d/nginx
/var/log/nginx/*.log {
    daily
    missingok
    rotate 30
    compress
    delaycompress
    notifempty
    create 640 nginx adm
    sharedscripts
    postrotate
        if [ -f /var/run/nginx.pid ]; then
            kill -USR1 `cat /var/run/nginx.pid`
        fi
    endscript
}
```

## 9. 安全加固清单

### 9.1 隐藏版本号

```nginx
http {
    server_tokens off;             # 不在响应头和错误页显示版本
    more_clear_headers 'Server';   # 如使用 headers-more 模块可彻底清除
}
```

### 9.2 限制请求体大小

```nginx
http {
    client_max_body_size 50m;      # 全局限制
}

# 对上传接口单独放宽
location /api/upload {
    client_max_body_size 500m;
    proxy_pass http://api_backend;
}
```

### 9.3 禁用不必要的 HTTP 方法

```nginx
if ($request_method !~ ^(GET|POST|PUT|DELETE|HEAD|OPTIONS|PATCH)$ ) {
    return 405;
}
```

### 9.4 安全响应头清单

```nginx
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
add_header X-Frame-Options "SAMEORIGIN" always;
add_header X-Content-Type-Options "nosniff" always;
add_header X-XSS-Protection "1; mode=block" always;
add_header Referrer-Policy "strict-origin-when-cross-origin" always;
add_header Content-Security-Policy "default-src 'self'" always;
```

### 9.5 其他加固项

- 限制每个 IP 并发连接：`limit_conn conn_limit 50;`
- 限制请求速率：`limit_req zone=api_limit burst=200 nodelay;`
- 禁止访问隐藏文件：`location ~ /\. { deny all; }`
- 监听端口最小权限运行：以 `nginx` 用户运行 worker
- 配置防火墙仅开放 80/443：`firewall-cmd --permanent --add-service={http,https}`

## 10. Nginx 常用命令

```bash
nginx                      # 启动
nginx -s stop              # 快速停止
nginx -s quit              # 优雅停止（处理完当前请求）
nginx -s reload            # 重载配置
nginx -s reopen            # 重新打开日志文件
nginx -t                   # 测试配置文件语法
nginx -T                   # 测试并打印完整配置
nginx -V                   # 查看版本和编译参数
nginx -c /path/to/conf     # 指定配置文件启动

# systemd 方式
systemctl start nginx
systemctl stop nginx
systemctl restart nginx
systemctl reload nginx
systemctl status nginx
systemctl enable nginx

# 平滑升级二进制
kill -USR2 $(cat /var/run/nginx.pid)
kill -WINCH $(cat /var/run/nginx.pid.oldbin)
kill -QUIT $(cat /var/run/nginx.pid.oldbin)
```

## 11. 常见问题排查

### 11.1 502 Bad Gateway

**原因**：后端服务不可达或拒绝连接。

**排查步骤**：

```bash
# 1. 检查后端服务是否存活
curl -v http://10.0.1.11:8080/health

# 2. 检查 Nginx 错误日志
tail -f /var/log/nginx/error.log | grep 502

# 3. 检查端口监听
ss -tlnp | grep 8080

# 4. 检查防火墙
firewall-cmd --list-all
iptables -L -n
```

**常见根因**：后端服务挂掉、端口被防火墙拦截、SELinux 限制（`setsebool -P httpd_can_network_connect 1`）。

### 11.2 504 Gateway Timeout

**原因**：后端响应超时。

**解决**：

```nginx
# 临时延长超时（针对慢接口）
location /api/export {
    proxy_read_timeout 300s;
    proxy_pass http://api_backend;
}
```

同时排查后端慢查询、GC、线程池满等问题。

### 11.3 413 Request Entity Too Large

**原因**：上传文件超过 `client_max_body_size`。

**解决**：

```nginx
location /api/upload {
    client_max_body_size 500m;
    proxy_pass http://api_backend;
}
```

### 11.4 499 Client Closed Request

**原因**：客户端主动断开连接（多为用户取消或前端超时）。

**处理**：若为正常情况可忽略；若为高频出现，需检查后端响应时间。

```nginx
# 防止后端慢导致 499
proxy_ignore_client_abort on;   # 谨慎使用
```

### 11.5 端口占用

```bash
# 查看 80 端口占用
ss -tlnp | grep :80
lsof -i :80

# 杀掉占用进程
kill -9 <PID>
```

### 11.6 配置语法错误

```bash
nginx -t
# 输出示例：
# nginx: [emerg] unknown directive "xxx" in /etc/nginx/nginx.conf:42
# nginx: configuration file /etc/nginx/nginx.conf test failed
```

## 12. 变更记录

| 版本 | 日期 | 修改内容 | 修改人 |
| --- | --- | --- | --- |
| v1.0 | 2022-06-10 | 初版发布 | 张磊 |
| v2.0 | 2023-08-20 | 增加 HTTP/2 与 TLS 1.3 配置 | 张磊 |
| v2.1 | 2023-11-05 | 补充安全加固章节 | 王芳 |
| v2.2 | 2024-01-12 | 增加 limit_req 限流配置 | 张磊 |
| v2.3 | 2024-03-15 | 增加 502/504/413 排查流程 | 张磊 |

## 13. 附录

### 13.1 状态监控

启用 stub_status 模块查看 Nginx 自身状态：

```nginx
server {
    listen 127.0.0.1:8090;
    location /nginx_status {
        stub_status on;
        access_log off;
        allow 127.0.0.1;
        deny all;
    }
}
```

访问 `http://127.0.0.1:8090/nginx_status` 返回：

```
Active connections: 245
server accepts handled requests
 8456 8456 328912
Reading: 3 Writing: 1 Waiting: 241
```

### 13.2 参考资料

- Nginx 官方文档：http://nginx.org/en/docs/
- Nginx 安全配置基线：CIS Nginx Benchmark
- 公司内部工单系统：https://jira.example.com/ops
