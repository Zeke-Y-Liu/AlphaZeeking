import { Body, Controller, Get, Post, Req, Res } from '@nestjs/common';
import { AxiosRequestConfig, AxiosResponse } from 'axios';
import { Request, Response } from 'express';
import { firstValueFrom, Observable } from 'rxjs';

import { CoreClientService } from '../http/core-client.service';

/** 非 2xx 亦作为正常响应原样透传（含 §6.3 统一错误体，如 401/429）。 */
const PASS_THROUGH: AxiosRequestConfig = { validateStatus: () => true };

/**
 * 认证控制器（T5.2）。
 * - `/api/auth/login`、`/api/auth/refresh`、`/api/auth/me` → 转发 core `/api/v1/auth/*`；
 * - 响应状态码与响应体（含错误体）原样透传，不做改写；
 * - `me` 透传原请求的 Authorization 头，由 core 校验。
 *
 * 路由优先级：本控制器必须在 ProxyController 之前注册（见 app.module），
 * 且 ProxyController 已显式排除 `/api/auth/*`。
 */
@Controller('api/auth')
export class AuthController {
  constructor(private readonly core: CoreClientService) {}

  @Post('login')
  async login(@Body() body: unknown, @Res() res: Response): Promise<void> {
    await this.forward(res, this.core.post('/api/v1/auth/login', body, PASS_THROUGH));
  }

  @Post('refresh')
  async refresh(@Body() body: unknown, @Res() res: Response): Promise<void> {
    await this.forward(res, this.core.post('/api/v1/auth/refresh', body, PASS_THROUGH));
  }

  @Get('me')
  async me(@Req() req: Request, @Res() res: Response): Promise<void> {
    const config: AxiosRequestConfig = { ...PASS_THROUGH };
    const auth = req.headers['authorization'];
    if (typeof auth === 'string') config.headers = { Authorization: auth };
    await this.forward(res, this.core.get('/api/v1/auth/me', config));
  }

  private async forward(res: Response, obs: Observable<AxiosResponse<unknown>>): Promise<void> {
    const upstream = await firstValueFrom(obs);
    res.status(upstream.status).json(upstream.data);
  }
}
