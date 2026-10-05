import { Controller, Get, HttpException, HttpStatus, Param } from '@nestjs/common';

/**
 * SSE 端点占位（T5.3）。
 *
 * `GET /api/v1/jobs/:id/events` 的事件流尚未实现，显式返回 501，
 * 错误体与 §6.3 同构 `{code,message,detail}`；此处引入新错误码 `NOT_IMPLEMENTED`。
 *
 * 路由优先级：本控制器须在 ProxyController 之前注册（见 app.module），
 * 使该显式路由优先命中，不落入 `@All('api/*')` 通配代理。
 */
@Controller()
export class SseController {
  @Get('api/v1/jobs/:id/events')
  events(@Param('id') id: string): never {
    throw new HttpException(
      {
        code: 'NOT_IMPLEMENTED',
        message: `SSE 事件流尚未实现：job ${id}`,
        detail: null,
      },
      HttpStatus.NOT_IMPLEMENTED,
    );
  }
}
