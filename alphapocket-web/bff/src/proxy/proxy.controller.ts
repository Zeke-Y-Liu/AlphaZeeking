import { All, Controller, NotFoundException, Req, Res } from '@nestjs/common';
import { AxiosRequestConfig } from 'axios';
import { Request, Response } from 'express';
import { Readable } from 'node:stream';
import { firstValueFrom } from 'rxjs';

import { CoreClientService } from '../http/core-client.service';

/** 需要从上游回传的响应头（含 iframe 嵌入与下载所需头）。 */
const FORWARD_RESPONSE_HEADERS = [
  'content-type',
  'content-disposition',
  'content-language',
  'cache-control',
  'x-frame-options',
  'etag',
  'last-modified',
];

/**
 * 通配代理控制器（T5.1）。
 * - 将 /api/* 原样转发到 core（路径含 query 原样、method/body 透传）；
 * - 响应以流式回传（responseType:'stream' + pipe），产物下载不落内存；
 * - 保留 core 的状态码与响应体（含 §6.3 错误体，原样透传）；
 * - 上游不可达 → 由全局异常过滤器映射为 502 UPSTREAM_UNAVAILABLE。
 *
 * 路由优先级：本控制器必须在 T5.2 的 AuthController 之后注册（见 app.module 注释），
 * 使 /api/auth/* 命中 T5.2 的显式路由而不落入本通配代理；下面的前缀兜底保证即使
 * AuthController 尚未注册也不会把 /api/auth/* 误代理到 core。
 */
@Controller()
export class ProxyController {
  constructor(private readonly core: CoreClientService) {}

  @All('api/*')
  async proxy(@Req() req: Request, @Res() res: Response): Promise<void> {
    const targetPath = req.originalUrl || req.url;

    // /api/auth 归 T5.2 所有：通配代理不吞该前缀
    if (targetPath === '/api/auth' || targetPath.startsWith('/api/auth/')) {
      throw new NotFoundException('/api/auth 由 T5.2 认证模块处理');
    }

    const method = req.method.toUpperCase();
    const headers: Record<string, string> = {};
    const rid = req.headers['x-request-id'];
    if (typeof rid === 'string' && rid !== '') headers['X-Request-Id'] = rid;
    if (typeof req.headers['content-type'] === 'string') {
      headers['content-type'] = req.headers['content-type'];
    }

    const config: AxiosRequestConfig = {
      method,
      url: targetPath, // CoreClientService 补全为 ${CORE_URL}${targetPath}
      headers,
      responseType: 'stream', // 流式透传
      validateStatus: () => true, // 非 2xx 亦作为正常响应透传，保持 core 错误体原样
    };
    if (method !== 'GET' && method !== 'HEAD') {
      config.data = req.body;
    }

    const upstream = await firstValueFrom(this.core.request(config));

    res.status(upstream.status);
    for (const name of FORWARD_RESPONSE_HEADERS) {
      const value = upstream.headers[name];
      if (typeof value === 'string') res.setHeader(name, value);
    }
    if (!res.getHeader('X-Request-Id') && typeof rid === 'string') {
      res.setHeader('X-Request-Id', rid);
    }

    const stream = upstream.data as Readable;
    // 客户端提前断开时释放上游流，避免句柄泄漏
    res.on('close', () => stream.destroy());
    stream.pipe(res);
  }
}
