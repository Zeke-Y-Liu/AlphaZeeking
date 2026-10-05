# syntax=docker/dockerfile:1
#
# bff 服务镜像（NestJS）
# 构建上下文 = /workspace（monorepo 父目录）
#   docker build -f alphapocket-web/bff/Dockerfile -t ap-bff /workspace

FROM node:20-alpine AS build
WORKDIR /app
RUN corepack enable
COPY alphapocket-web/bff/package.json alphapocket-web/bff/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY alphapocket-web/bff/ ./
RUN pnpm build

FROM node:20-alpine AS run
WORKDIR /app
ENV NODE_ENV=production \
    PORT=8081
RUN corepack enable
COPY --from=build /app/package.json /app/pnpm-lock.yaml ./
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/dist ./dist
EXPOSE 8081
CMD ["node", "dist/main.js"]
