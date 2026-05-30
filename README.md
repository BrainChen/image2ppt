# img2ppt MVP

一个 7 天内可验证价值的 Image → VLM Slide Parser → Slide AST → PptxGenJS → Editable PPTX 最小闭环。

## 架构

```text
Image
  ↓
VLM Layout Parser + OCR
  ↓
Slide AST(JSON)
  ↓
Style Extractor + Image/Icon Crops
  ↓
PptxGenJS Compiler
  ↓
Editable PPTX
```

第一版只重建 5 种元素：

- `text`
- `rect`
- `rounded_rect`
- `image`
- `line`

复杂 icon、logo、diagram 会裁剪为图片插入；文本、矩形、圆角矩形和线条会重建为原生 PPT 对象。

## 快速开始

```bash
cp .env.example .env
pip install -r requirements.txt
npm install
```

填写 `.env`：

```env
VLM_MODEL_NAME=qwen2.5-vl-72b-instruct
VLM_MODEL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
VLM_MODEL_API_KEY=your_vlm_api_key

REASONING_MODEL_NAME=qwen3-235b-a22b
REASONING_MODEL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
REASONING_MODEL_API_KEY=your_reasoning_api_key

OCR_FAILURE_MODE=continue

LOG_LEVEL=INFO
LOG_FILE=outputs/logs/img2ppt.log
```

模型分工：

- `VLM_*`：给 `LayoutAgent` 和 `OcrAgent` 使用，必须支持图片输入。
- `REASONING_*`：给 `ReasoningAgent` 使用，只处理 AST JSON，不需要图片能力。
- 只要设置了 `REASONING_MODEL_NAME`，pipeline 默认会启用 AST 推理修正；也可以用 `--skip-reasoning` 关闭。
- `OCR_FAILURE_MODE`：OCR 失败策略；默认 `continue`，会保留 layout 已提取文本继续生成 PPT。
- `LOG_*`：控制系统日志；默认写入 `outputs/logs/img2ppt.log`，不会记录 API key 或图片 base64。

运行转换：

```bash
python -m backend.pipeline images/1.jpg --out outputs/ppt/1.pptx
```

显式启用或关闭推理修正：

```bash
python -m backend.pipeline images/1.jpg --use-reasoning --out outputs/ppt/1.pptx
python -m backend.pipeline images/1.jpg --skip-reasoning --out outputs/ppt/1.pptx
```

如果 OCR 接口报 400，但 Layout Parser 已经提取出文本，可以继续生成：

```bash
python -m backend.pipeline images/good2.jpg \
  --ocr-failure-mode continue \
  --out outputs/ppt/good2.pptx
```

如果你想严格失败并排查 OCR 请求：

```bash
python -m backend.pipeline images/good2.jpg \
  --ocr-failure-mode fail \
  --out outputs/ppt/good2.pptx
```

指定日志级别或日志文件：

```bash
python -m backend.pipeline images/good1.jpg \
  --log-level DEBUG \
  --log-file outputs/logs/debug.log \
  --artifact-dir outputs/intermediates/debug-run \
  --out outputs/ppt/good1.pptx
```

查看最近日志：

```bash
tail -f outputs/logs/img2ppt.log
```

输出：

- `outputs/ast/1_ast.json`：Single Source of Truth
- `outputs/crops/1/`：icon/logo/diagram 裁剪图
- `outputs/intermediates/1/<run_id>/`：每一步中间产物
- `outputs/ppt/1.pptx`：可编辑 PPT

中间产物目录会保存：

- `00_request.json`：本次转换入参、输出路径和开关。
- `01_image.json`：输入图片尺寸。
- `02_*`：Layout Parser 原始响应、模型信息和 layout 后 AST。
- `03_*`：OCR 原始响应、OCR items 和 OCR 合并后 AST。
- `04_*`：Reasoning Refiner 原始响应、模型信息和推理修正后 AST。
- `05_after_style_ast.json`：OpenCV 样式提取后的 AST。
- `06_after_image_extract_ast.json` / `06_crops.json`：图片裁剪后的 AST 和 crop 列表。
- `07_final_ast.json`：最终写入 `outputs/ast` 前的 AST。
- `*_boxes.png`：把对应阶段 AST 的 `bbox: [x, y, w, h]` 叠加画在原图上，用于排查元素位置。
- `08_compile.json`：PPT 编译结果。

## 不调用模型的烟测

用于验证 AST、样式提取、裁剪和 PPT 编译链路：

```bash
python -m backend.pipeline images/1.jpg --mock-layout --skip-ocr --out outputs/ppt/mock.pptx
```

## 使用已有 Layout JSON

如果你已经有 VLM 输出，可以跳过 Layout Agent：

```bash
python -m backend.pipeline images/1.jpg \
  --layout-json output/1_modules.json \
  --out outputs/ppt/1.pptx
```

## Upload API

```bash
uvicorn backend.server:app --reload
```

```bash
curl -F "file=@images/1.jpg" -o result.pptx http://127.0.0.1:8000/convert
```

## Slide AST 示例

```json
{
  "slide": {
    "width": 13.33,
    "height": 7.5,
    "image_width": 1600,
    "image_height": 900,
    "children": [
      {
        "id": "node_1",
        "type": "rounded_rect",
        "bbox": [100, 200, 300, 120],
        "style": {
          "fill": "#ffffff",
          "stroke": "#7a61ff",
          "radius": 12
        },
        "children": [
          {
            "id": "txt_1",
            "type": "text",
            "bbox": [120, 220, 250, 40],
            "text": "AI Engine",
            "style": {
              "fontSize": 18,
              "fontWeight": "bold"
            }
          }
        ]
      }
    ]
  }
}
```

## 模块说明

- `backend/agents/layout_agent.py`：调用 Qwen2.5-VL/OpenAI-compatible VLM 输出层级布局 JSON。
- `backend/agents/ocr_agent.py`：只抽取文本与 bbox，并合并回 AST。
- `backend/agents/reasoning_agent.py`：调用独立推理模型修正 AST 层级、类型和重复节点。
- `backend/agents/style_agent.py`：用 OpenCV 从图像区域提取 fill、stroke、文字颜色和字号估计。
- `backend/agents/image_agent.py`：裁剪 icon/logo/diagram 等复杂元素。
- `backend/ast/slide_ast.py`：Pydantic Slide AST schema 与归一化逻辑。
- `backend/compiler/ppt_compiler.ts`：PptxGenJS 编译器，将 AST 渲染为可编辑 PPTX。
- `backend/pipeline.py`：CLI 和 Python 可调用转换入口。
- `backend/server.py`：FastAPI 上传转换接口。
- `backend/utils/logging_config.py`：初始化系统日志，记录 pipeline 阶段、耗时、模型名和输出路径。
