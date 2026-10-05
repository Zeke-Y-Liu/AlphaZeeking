# syntax=docker/dockerfile:1
#
# web 服务镜像（多阶段：node 构建 → nginx 托管）
# 构建上下文 = /workspace（monorepo 父目录）
#   docker build -f alphapocket-web/web/Dockerfile -t ap-web /workspace

FROM node:20-alpine AS build
WORKDIR /app
RUN corepack enable
COPY alphapocket-web/web/package.json alphapocket-web/web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY alphapocket-web/web/ ./
RUN pnpm build

FROM nginx:1.27-alpine AS run
COPY --from=build /app/dist /usr/share/nginx/html
COPY alphapocket-web/web/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
