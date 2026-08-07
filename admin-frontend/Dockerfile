# 生产阶段：直接使用本地构建的 dist/
# 构建方式：先在本地 npm run build，再 docker build
FROM nginx:alpine

COPY dist /usr/share/nginx/html

COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
