# img2ppt

[中文文档](README.zh-CN.md)

Reconstruct slide images into editable `.pptx` files.

## Results

The repository includes three examples showing the full flow: raw camera photo → preprocessed slide image → reconstructed PPTX preview.

| Raw Photo | Preprocessed Image | PPTX Preview |
| --- | --- | --- |
| <img src="raw-images/1.jpg" width="280" alt="raw image 1"> | <img src="images/good1.jpg" width="280" alt="preprocessed image 1"> | <img src="display/1.jpg" width="280" alt="ppt display 1"> |

Directory notes:

- `raw-images/`: raw photos, such as projector, screen, or conference-room captures.
- `images/`: preprocessed slide-like images, usually better inputs for reconstruction.
- `display/`: exported screenshots of final PPTX files, used only for visual demos.
- `outputs/`: actual runtime outputs, including AST files, crops, intermediate artifacts, logs, and `.pptx` files.

## Installation

```bash
cp .env.example .env
pip install -r requirements.txt
npm install
```

Fill in model settings in `.env`:

```env
VLM_MODEL_NAME=qwen2.5-vl-72b-instruct
VLM_MODEL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
VLM_MODEL_API_KEY=your_vlm_api_key

REASONING_MODEL_NAME=qwen3-235b-a22b
REASONING_MODEL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
REASONING_MODEL_API_KEY=your_reasoning_api_key

OCR_FAILURE_MODE=continue

NANOBANANA_PRO_MODEL_NAME=google/gemini-3-pro-image-preview
NANOBANANA_PRO_BASE_URL=https://openrouter.ai/api/v1
NANOBANANA_PRO_API_KEY=your_openrouter_or_compatible_api_key

LOG_LEVEL=INFO
LOG_FILE=outputs/logs/img2ppt.log
```

## Run

### Convert one clean slide image

```bash
python -m backend.pipeline images/good1.jpg --out outputs/ppt/good1.pptx
```

### Batch convert `images/`

```bash
python -m backend.pipeline images --out outputs/ppt
```

### Start from raw photos

When the input path is `raw-images/`, the pipeline automatically preprocesses each raw photo before recognition and PPTX generation.

```bash
python -m backend.pipeline raw-images --out outputs/ppt
```

You can also manually enable or disable raw image preprocessing:

```bash
python -m backend.pipeline raw-images --preprocess-raw-images --out outputs/ppt
python -m backend.pipeline raw-images --no-preprocess-raw-images --out outputs/ppt
```

### Smoke test without model calls

Use this to verify the local pipeline:

```bash
python -m backend.pipeline images/good1.jpg \
  --mock-layout \
  --skip-ocr \
  --out outputs/ppt/mock.pptx
```

## Runtime Logic

### 1. Input

The input can be a single image or a directory.

- `images/good1.jpg`: converts one image.
- `images/`: converts images one by one, sorted by filename.
- `raw-images/`: preprocesses every photo first, then generates PPTX files.

Supported image formats:

```text
.png .jpg .jpeg .webp .bmp .tif .tiff
```

### 2. Raw Image Preprocessing

Raw image preprocessing is enabled when:

- the input directory is named `raw-images`
- the input image is inside `raw-images/`
- `--preprocess-raw-images` is passed explicitly

It converts camera photos into cleaner PPT-style slide images. The generated image is saved under:

```text
outputs/intermediates/<name>_<timestamp>/00_raw_image_preprocess/
```

### 3. Recognition and Reconstruction

The pipeline runs these steps in order:

```text
read image size
→ parse layout with VLM
→ supplement text with OCR
→ refine AST with reasoning model
→ extract styles with OpenCV
→ crop complex image elements
→ save final AST
→ compile PPTX
```

The generated PPTX keeps objects editable where possible:

- text becomes editable PowerPoint text boxes
- rectangles become PowerPoint shapes
- rounded rectangles become PowerPoint shapes
- lines become PowerPoint lines
- logos, icons, and complex diagrams are cropped and inserted as images

### 4. Outputs

Default outputs include timestamps to avoid overwriting previous runs.

Common outputs:

- `outputs/ppt/*.pptx`: final editable PPTX files.
- `outputs/ast/*_ast.json`: final Slide AST files.
- `outputs/crops/*/`: cropped complex elements.
- `outputs/intermediates/*/`: step-by-step artifacts and debug images.
- `outputs/logs/img2ppt.log`: runtime logs.

Useful intermediate files:

- `02_after_layout_boxes.png`: layout bbox visualization.
- `03_after_ocr_boxes.png`: OCR-merged bbox visualization.
- `05_after_style_ast.json`: AST after style extraction.
- `06_crops.json`: cropped image element list.
- `07_final_ast.json`: final AST.
- `08_compile.json`: PPTX compilation result.

## Common Options

```bash
# Skip OCR
python -m backend.pipeline images/good1.jpg --skip-ocr --out outputs/ppt/good1.pptx

# Force reasoning refinement
python -m backend.pipeline images/good1.jpg --use-reasoning --out outputs/ppt/good1.pptx

# Disable reasoning refinement
python -m backend.pipeline images/good1.jpg --skip-reasoning --out outputs/ppt/good1.pptx

# Generate only AST and crops, without PPTX compilation
python -m backend.pipeline images/good1.jpg --skip-compile

# Specify the intermediate artifact directory
python -m backend.pipeline images/good1.jpg \
  --artifact-dir outputs/intermediates/debug-run \
  --out outputs/ppt/good1.pptx
```

## API

When exposing the service publicly, configure an access token in `.env`:

```bash
IMG2PPT_ACCESS_TOKEN=replace-with-a-long-random-token
```

Protected API endpoints and intermediate output files require the `access_token` parameter. The frontend includes an input field and sends it automatically.

Start the server:

```bash
uvicorn backend.server:app --reload
```

Build and open the frontend:

```bash
npm run build:frontend
uvicorn backend.server:app --reload
open http://127.0.0.1:8000
```

The frontend creates async jobs with `POST /api/jobs` and polls `GET /api/jobs/{job_id}`. It shows the Nanobanana image from
`00_raw_image_preprocess/`, `02_after_layout_boxes.png`, the final PPT preview screenshot, and a PPTX download link when ready.

Upload an image and receive a PPTX:

```bash
curl -F "access_token=$IMG2PPT_ACCESS_TOKEN" -F "file=@images/good1.jpg" -o result.pptx http://127.0.0.1:8000/convert
```

Health check:

```bash
curl http://127.0.0.1:8000/health
```

## License

MIT License. See [LICENSE](LICENSE).
