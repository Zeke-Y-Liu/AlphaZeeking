/**
 * ECharts 按需引入（T6.5）：仅注册回测页所需的折线图（LineChart）+ 网格/提示/图例组件
 * + Canvas 渲染器，避免整包引入拖大产物。命名与 echarts 5 的 `echarts/core` 导出保持一致。
 */
import * as echarts from 'echarts/core';
import { LineChart } from 'echarts/charts';
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';

echarts.use([LineChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer]);

export default echarts;
export type { ECharts } from 'echarts/core';
