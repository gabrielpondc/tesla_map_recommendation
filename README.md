# 电途 · EV 充电路线规划

一个 Vue 3 + Python 的电动车长途路线规划原型。它保留高德、百度两套实时地图，可在启动时选择，并把“高速服务区充电”和“下高速后附近的 Tesla 超充”放在一起比较。

## 已实现

- 输入或搜索起点、目的地，支持浏览器当前位置定位
- 输入起始可用续航（km）
- 输入基础安全余量（km）；系统会根据车辆满电续航向上推荐 45、50、60 km 等更稳妥余量，但不会自动低于基础值
- 设置车辆满电续航、每站充至百分比、最大下高速距离
- 综合最优、Tesla 超充优先、服务区优先三种策略
- 可选择“传统算法”或“AI 推荐”；传统模式不会调用 GPT
- 沿整条路线搜索 Tesla 超充、服务区充电和公共快充；Tesla 超充按出口到站、充电后返回入口的实际往返距离与时间比较
- 当站间距离较长时自动把充电目标提高到 90%/100%，并把下高速范围分级放宽到最多 30 km
- AI 推荐使用 GPT JSON Schema 结构化输出，方案不合规时自动退回传统算法
- 展示总里程、分段里程、驾驶时间、补能时间、到站/到达续航
- 可通过手机系统分享整段路线及全部充电途经点
- 简洁的浅色界面，支持桌面与移动端单栏布局
- 未配置高德地图 Key 时自动使用演示数据，便于直接体验

## 快速启动

需要 Node.js 20+ 和 Python 3.9+。

```bash
python3 -m pip install -r backend/requirements.txt
cd frontend && npm install && cd ..
cp backend/.env.example backend/.env
./start.sh baidu
```

浏览器打开 <http://127.0.0.1:5173>。

如果 8001 或 5173 端口已占用，可以这样启动：

```bash
BACKEND_PORT=8010 FRONTEND_PORT=5180 ./start.sh
```

启动脚本会自动让前端连接到你指定的后端端口。使用百度地图时运行：

```bash
./start.sh baidu
```

不传地图参数时读取 `backend/.env` 中的 `MAP_PROVIDER`，项目默认以百度地图启动；也仍支持 `./start.sh amap` 随时切换到高德。两套地图代码和 Key 配置会同时保留，切换不需要改代码。

## 配置百度地图

在 `backend/.env` 中分别填写百度 JavaScript API AK 和 Web 服务 AK：

```dotenv
BAIDU_AK=你的百度地图AK
BAIDU_WEB_AK=你的百度Web服务AK
```

随后用 `./start.sh baidu` 启动。JavaScript AK 用于底图和道路折线；输入地点推荐、沿整条路线检索充电站以及高速出口/入口往返距离均由 Python 后端使用 Web 服务 AK 完成，服务端 AK 不会发送到前端。

高德模式同样由 Python 后端调用百度 Web 服务检索沿途充电站，再转换为高德坐标返回前端；底图和道路仍使用高德。这样两种模式都不会受前端 JS 地点检索日额度影响。

## 配置高德地图

在[高德开放平台控制台](https://console.amap.com/dev/key/app)创建应用，并按 `backend/.env.example` 填写：

```dotenv
AMAP_WEB_KEY=你的Web服务Key
AMAP_JS_KEY=你的JS API Key
AMAP_SECURITY_CODE=你的JS API安全密钥

# 可选：启用页面中的 AI 推荐模式
AI_API_BASE_URL=你的OpenAI兼容接口地址
AI_API_KEY=你的接口密钥
AI_MODEL=gpt-5.4
AI_TIMEOUT_SECONDS=18
```

- `AMAP_WEB_KEY` 供 Python 后端请求地点检索、逆地理编码和驾车路线，是可选的服务端备用能力；需要创建“Web服务”平台类型的 Key。
- `AMAP_JS_KEY` 和 `AMAP_SECURITY_CODE` 供浏览器加载 JavaScript API 2.0。
- Web 服务 Key 不会发送到浏览器；JS API Key 会由 `/api/config` 返回，这是浏览器地图 SDK 的正常用法。
- `AI_TIMEOUT_SECONDS` 是 AI 单次等待秒数，默认 18 秒；超时、网络失败或非 JSON 返回都会自动回退传统算法，不会让整条路线失败。

保存 `backend/.env` 后用 `./start.sh amap` 启动，即可使用真实高德地图数据。

## Tesla 分享说明

在手机浏览器点击“分享整段路线”后，网页会打开系统分享面板，内容包括起点、全部充电途经点和目的地。桌面浏览器不支持系统分享时，网页会复制整段行程与当前所选地图的地点链接。

需要注意：Tesla 的普通系统分享入口通常只把一个目的地写入车辆导航。网页可以分享完整多站行程，但 Tesla App 是否自动导入全部途经点取决于其当前版本；要保证一键写入车辆的多站导航，需要后续接入 Tesla Fleet API、车主 OAuth 和车辆授权。

## 规划方法

前端先从启动时选定的高德或百度地图取得真实驾车路线，每约 40 km 检索一次服务区充电站、公共快充与 Tesla 超充。对于下高速超充，还会搜索附近出口、收费站与高速入口，分别计算下高速到超充、充电后重新上高速的实际驾车距离和时间。Python 后端把候选点投影到路线进度，构建一个只向前的路线图，在到达每个充电站前满足以下条件：

```text
分段里程 ≤ 出发可用续航 − 用户设定的预留续航
```

预留续航约束用于“到充电站时还剩多少电”。前一站充完只要能直接到达终点，就不会为了让终点仍保留同样余量而增加一个多余站。

然后根据充电次数、出口到超充站的距离和用户偏好寻找总代价最低的可行方案。选出 0、1、2 或更多充电站后，高德会把所有站作为途经点重新计算最终道路、总里程和分段续航。出发前仍应在 Tesla App 中确认站点开放状态与拥挤度。

传统模式完全由本地算法计算。AI 模式会先生成传统算法基线，再把沿途全部 Tesla 超充及少量公共快充作为 JSON 发给 GPT，其中包括站点路线进度、下高速出口、出口到超充的公里数/分钟数、充电后返回高速入口的公里数/分钟数、往返合计、预计到站续航与百分比，以及分别充到 80%/90%/100% 的预计时间。请求使用严格 `json_schema`，响应只能包含充电目标、下高速范围、基础余量、真实候选站 ID、待继续搜索的路线进度和固定原因码。后端会删除不存在的站点 ID，并重新校验每段续航；如果前一站充完已经能到终点，会自动删除后续多余站。GPT 推荐的补能区段会明确标为“站点待确认”，不会伪装成真实充电站；接口不可用、返回自然语言或方案不可行时自动退回传统算法。

## 项目结构

```text
backend/
  app/main.py       API 与地点搜索
  app/amap.py       高德地图开放平台客户端
  app/planner.py    续航约束与充电站优化
  tests/            规划器测试
frontend/
  src/App.vue       规划表单与行程详情
  src/components/   地点输入与路线地图
  src/services/     高德、百度地图与启动分发
```

## 验证

```bash
cd backend && python3 -m unittest discover -s tests -v
cd frontend && npm run build
```
