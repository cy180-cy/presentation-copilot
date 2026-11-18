# Presentation Copilot：修改版源码与开源说明

本文件可随公开源码提供。它不包含内部进度、部署历史、真实配置、调用或费用记录。

## 来源、修改与许可

本项目基于 [Anionex/banana-slides](https://github.com/Anionex/banana-slides) 修改，保留原作者和贡献者的版权声明及根目录 GNU AGPL v3 许可证 `LICENSE`。

修改范围概述：产品界面与创作流程、匿名访客及作品归属、任务和版本追踪、预算与暂停管理、普通图片式PPTX/PDF导出、请求/执行/文件资源保护，以及单实例运行和维护工具。此处概述修改类别，不是模型质量或公网验收声明。

远程用户应能免费获取实际运行版本对应的修改版源码。上游地址只用于注明来源，不能代替本修改版源码。公开材料包含必要源码、构建/运行脚本、依赖声明与锁文件、可公开配置示例；不提供使用者的作品、账号、私有数据或任何凭据。各依赖仍保留各自许可，本材料不宣称已完成全部法律合规审查。

## 产品中的入口与唯一配置来源

页脚「获取本版本源码」进入 `/about#source-code`，关于页保留来源和AGPL原文，并链接开源依赖声明。

公开静态文件 `frontend/public/open-source/source-offer.json` 是源码链接及版本标识的唯一配置来源：

```json
{
  "url": "",
  "version": ""
}
```

两项为空时，页面显示「本版本源码获取地址待正式公开服务时补齐，当前尚未提供公开下载链接」，不会显示虚构下载地址。读取失败、字段无效也使用同样回退。有有效地址及版本标识时，显示「获取本版本对应的修改版源码」及该源码版本。配置只含公开信息；它不是管理后台设置，也不读取 `.env`、模型配置或运行授权。

以后正式公开前填写真实的、免登录且免费可获取的**本修改版**源码地址，以及对应提交或发行标识。支持 HTTPS 地址，或同站 `/open-source/` 下的地址；不接受凭据、查询参数、协议相对地址或非HTTPS外链。版本标识限1–80位英文字母、数字、点、下划线和横线。不要填示例域名、上游仓库首页、带访问令牌的地址或会失效的临时签名链接。

构建时该文件复制到 `.copilot/frontend-dist/open-source/source-offer.json`。修改源文件后按下节构建，或在日后获准发布时同步对应静态副本；仅修改仓库中的JSON不会自动改变已运行的网站。保持源文件和发行副本一致。配置的源码版本必须与实际发布字节对应，不能把父提交或旧部署包标成最新源码。本地占位入口不是已经完成公开源码提供。

## 独立的公开源码准备流程

使用 `scripts/source_offer.py`，不复用内部部署ZIP。默认只读检查并报告数量，不打包、不读取运行目录、不连接网络。

保留：根许可证、上游README/贡献说明、Python依赖声明与锁文件、后端实现及迁移脚本、前端源码/锁文件/受管构建配置、仓库自带公共模板/字体、相关脚本、隔离测试，以及本文件。仅保留显式审查的 `.env.copilot.example` 和server/systemd/Nginx示例；示例中的凭据为空、地址为占位。

排除：整个 `.copilot/` 运行树、`.git/`历史、依赖目录、缓存和构建产物；真实 `.env`、本机及生产runtime、密钥/证书/私钥；数据库、会话、账号、作品、上传、导出、历史生成内容、账本、调用/费用记录、日志及备份。内部 `docs/PROJECT_PLAN.md`、`docs/ADMIN.md`、`docs/DEPLOYMENT.md` 不进入公开候选，避免公开其中的操作历史和标识。原文档仍保留在内部工作区。上游会启停进程、含凭据形态模拟值的 `frontend/e2e/` 测试也不纳入该最小候选。源码内部的通用计费/权限实现与合成测试不是生产记录，不做破坏实现的批量替换。

PowerShell，仓库根目录执行，只生成本地审查清单：

```powershell
.\.venv\Scripts\python.exe -X utf8 -I -B scripts/source_offer.py --plan .copilot/source-offer/review-plan.json
```

脚本仅从Git已跟踪文件及明确审核的新文件收集，拒绝未知新增源码、链接/越界路径、部分常见凭据形态及异常大文件；凭据示例必须留空，server示例保持preview/无授权/占位地址。一份已人工核对的隔离追踪测试使用假Key验证脱敏，仅允许其精确内容摘要，不豁免后续变化或一般密钥。清单记录相对路径、大小和SHA256；不包含本机绝对路径、账户或运行状态。它是工作区字节快照，不能把基线HEAD当作包含未提交修改的版本。输出仅允许新的 `.copilot/source-offer/` 文件，不覆盖已有材料。

人工逐项审查清单及差异，尤其是硬编码敏感内容、合成测试与真实数据的区别、版权和第三方通知。自动检测并不能证明不存在任何秘密；出现疑似内容时拒绝并人工处理，不静默脱敏业务源码。必要源码必须保持与对应运行版本一致。

确认版本及内容后，以下命令才显式产生**本地候选包**，仍不上传或发布：

```powershell
.\.venv\Scripts\python.exe -X utf8 -I -B scripts/source_offer.py --archive .copilot/source-offer/source-candidate.zip --version REPLACE_WITH_REVIEWED_VERSION
```

ZIP内提供 `source-manifest.json` 及逐文件摘要，写入时再次核对内容；新文件需人工审查后加入清单，禁止为了生成包而绕过拒绝。候选包仍需人工复核，发布时保存完整SHA256、实际源码提交/版本、前端构建摘要及实际源码URL。对外提供时可选公开Git仓库的固定提交/标签或免登录源码下载包；不要公开内部Git历史。不要运行部署打包器来代替此流程。

## 构建、安装和本地预览

前提：Python 3.11、Node.js及npm。依赖安装由源码接收者自行执行，脚本不会自动安装：

```text
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r deploy/requirements.txt
cd frontend
npm ci
node ./node_modules/vite/bin/vite.js build --config vite.copilot.config.ts
cd ..
```

Linux下Python路径使用 `.venv/bin/python`。受管前端构建不加载 `.env`，输出 `.copilot/frontend-dist`。根 `pyproject.toml` 保留上游可选能力依赖，`deploy/requirements.txt` 是当前单实例核心运行子集，不是所有上游能力均已启用的承诺。

仅在新的个人目录，将 `.env.copilot.example` 复制为 `.env.copilot`，保持Key为空；运行 `scripts/presentation_copilot.py check` 后，可用 `serve` 本地预览。该入口默认关闭模型调用，数据写入 `.copilot/`，只绑定环回地址。不要复用他人的真实配置、数据库、签名或授权。本文件不指导启动/覆盖现有生产服务。

## 开源依赖材料与剩余核对范围

产品内 `/open-source/THIRD_PARTY_NOTICES.txt` 汇集当前直接前端运行依赖、核心后端依赖及内置字体的版本、许可说明和现有原文。来源是本地安装包元数据及原许可文件；少数安装包未附原文，使用官方版权声明配合通用MIT条款，明确标注格式不是上游逐字节下载副本。SIL字体许可按官方文本及字体内嵌版权保留。声明中的`main`来源须在正式发行前与锁定版本再次核对，不能将字体改标为AGPL。

当前清单不是完整依赖审计：传递依赖、构建工具、其他可选能力，以及依赖本身的对应源码提供要求，须按实际分发内容核对。保留原许可中的版权、免责声明及NOTICE；依赖升级后同步更新清单。不要因为已有一份声明就宣称所有许可义务已完成。
