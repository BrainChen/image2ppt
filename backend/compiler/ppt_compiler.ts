import fs from "node:fs";
import path from "node:path";
import pptxgenjs from "pptxgenjs";

const pptxModule = pptxgenjs as any;
const PptxGenJS = (pptxModule.default || pptxModule) as { new (): any };

type ElementType = "text" | "rect" | "rounded_rect" | "image" | "line";

interface Style {
  fill?: string | null;
  stroke?: string | null;
  strokeWidth?: number | null;
  radius?: number | null;
  fontSize?: number | null;
  fontWeight?: string | null;
  fontFamily?: string | null;
  color?: string | null;
  align?: "left" | "center" | "right" | null;
  opacity?: number | null;
}

interface ElementNode {
  id: string;
  type: ElementType;
  bbox: [number, number, number, number];
  parent_id?: string | null;
  text?: string | null;
  image_path?: string | null;
  style?: Style | null;
  children?: ElementNode[];
  metadata?: Record<string, unknown>;
}

interface SlideAst {
  slide: {
    width: number;
    height: number;
    image_width?: number | null;
    image_height?: number | null;
    background?: Style | null;
    children: ElementNode[];
  };
  metadata?: Record<string, unknown>;
}

interface Args {
  input: string;
  output: string;
}

const args = parseArgs(process.argv.slice(2));
const astPath = path.resolve(args.input);
const outPath = path.resolve(args.output);
const ast = JSON.parse(fs.readFileSync(astPath, "utf8")) as SlideAst;

await compile(ast, astPath, outPath);

async function compile(document: SlideAst, sourcePath: string, outputPath: string): Promise<void> {
  const pptx = new PptxGenJS();
  const slideWidth = Number(document.slide.width || 13.33);
  const slideHeight = Number(document.slide.height || 7.5);
  pptx.defineLayout({ name: "IMG2PPT_LAYOUT", width: slideWidth, height: slideHeight });
  pptx.layout = "IMG2PPT_LAYOUT";
  pptx.author = "img2ppt";
  pptx.subject = "Editable PPT reconstructed from a slide image";
  pptx.company = "img2ppt";
  pptx.lang = "zh-CN";
  pptx.theme = {
    headFontFace: "Aptos Display",
    bodyFontFace: "Aptos",
    lang: "zh-CN",
  };
  pptx.margin = 0;

  const slide = pptx.addSlide();
  const background = normalizeHex(document.slide.background?.fill);
  if (background) {
    slide.background = { color: background };
  }

  const context = {
    astDir: path.dirname(sourcePath),
    imageWidth: Number(document.slide.image_width || 0),
    imageHeight: Number(document.slide.image_height || 0),
    slideWidth,
    slideHeight,
  };

  for (const node of document.slide.children || []) {
    renderNode(slide, pptx, node, context);
  }

  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  await pptx.writeFile({ fileName: outputPath });
  console.log(`Wrote ${outputPath}`);
}

function renderNode(
  slide: any,
  pptx: any,
  node: ElementNode,
  context: {
    astDir: string;
    imageWidth: number;
    imageHeight: number;
    slideWidth: number;
    slideHeight: number;
  },
): void {
  const box = mapBox(node.bbox, context);
  if (box.w <= 0 || box.h <= 0) {
    return;
  }

  const singleTextChild = singleDirectTextChild(node);
  if ((node.type === "rect" || node.type === "rounded_rect") && singleTextChild) {
    renderShapeWithText(
      slide,
      node.type === "rect" ? pptx.ShapeType.rect : pptx.ShapeType.roundRect,
      node,
      singleTextChild,
      box,
    );
    return;
  }

  switch (node.type) {
    case "text":
      renderText(slide, node, box);
      break;
    case "rect":
      renderShape(slide, pptx.ShapeType.rect, node, box);
      break;
    case "rounded_rect":
      renderShape(slide, pptx.ShapeType.roundRect, node, box);
      break;
    case "image":
      renderImage(slide, node, box, context.astDir);
      break;
    case "line":
      renderLine(slide, pptx, node, box);
      break;
    default:
      break;
  }

  for (const child of node.children || []) {
    renderNode(slide, pptx, child, context);
  }
}

function singleDirectTextChild(node: ElementNode): ElementNode | null {
  const children = node.children || [];
  if (children.length !== 1) {
    return null;
  }
  const child = children[0];
  if (child.type !== "text" || child.children?.length) {
    return null;
  }
  return child.text?.trim() ? child : null;
}

function renderText(
  slide: any,
  node: ElementNode,
  box: { x: number; y: number; w: number; h: number },
): void {
  const style = node.style || {};
  const text = node.text || "";
  if (!text.trim()) {
    return;
  }
  const fontSize = Number(style.fontSize || Math.max(6, Math.min(36, box.h * 72 * 0.55)));
  const bold = style.fontWeight === "bold" || Number(style.fontWeight) >= 600;
  slide.addText(text, {
    x: box.x,
    y: box.y,
    w: box.w,
    h: box.h,
    margin: 0.03,
    fit: "shrink",
    valign: "mid",
    align: style.align || "center",
    fontFace: style.fontFamily || "Aptos",
    fontSize,
    bold,
    color: normalizeHex(style.color) || "111111",
    breakLine: false,
    paraSpaceAfterPt: 0,
  });
}

function renderShape(
  slide: any,
  shapeType: any,
  node: ElementNode,
  box: { x: number; y: number; w: number; h: number },
): void {
  const style = node.style || {};
  const fillColor = normalizeHex(style.fill);
  const strokeColor = normalizeHex(style.stroke);
  const strokeWidth = Number(style.strokeWidth ?? (strokeColor ? 0.75 : 0));
  slide.addShape(shapeType, {
    x: box.x,
    y: box.y,
    w: box.w,
    h: box.h,
    fill: fillColor ? { color: fillColor, transparency: opacityToTransparency(style.opacity) } : transparentFill(),
    line:
      strokeColor && strokeWidth > 0
        ? { color: strokeColor, width: strokeWidth }
        : { color: "FFFFFF", transparency: 100, width: 0 },
  });
}

function renderShapeWithText(
  slide: any,
  shapeType: any,
  node: ElementNode,
  textNode: ElementNode,
  box: { x: number; y: number; w: number; h: number },
): void {
  const shapeStyle = node.style || {};
  const textStyle = textNode.style || {};
  const fillColor = normalizeHex(shapeStyle.fill);
  const strokeColor = normalizeHex(shapeStyle.stroke);
  const strokeWidth = Number(shapeStyle.strokeWidth ?? (strokeColor ? 0.75 : 0));
  const fontSize = Number(textStyle.fontSize || Math.max(6, Math.min(36, box.h * 72 * 0.38)));
  const bold = textStyle.fontWeight === "bold" || Number(textStyle.fontWeight) >= 600;
  slide.addText(textNode.text || "", {
    x: box.x,
    y: box.y,
    w: box.w,
    h: box.h,
    shape: shapeType,
    margin: 0.04,
    fit: "shrink",
    valign: "mid",
    align: textStyle.align || "center",
    fontFace: textStyle.fontFamily || "Aptos",
    fontSize,
    bold,
    color: normalizeHex(textStyle.color) || "111111",
    fill: fillColor ? { color: fillColor, transparency: opacityToTransparency(shapeStyle.opacity) } : transparentFill(),
    line:
      strokeColor && strokeWidth > 0
        ? { color: strokeColor, width: strokeWidth }
        : { color: "FFFFFF", transparency: 100, width: 0 },
    paraSpaceAfterPt: 0,
  });
}

function renderImage(
  slide: any,
  node: ElementNode,
  box: { x: number; y: number; w: number; h: number },
  astDir: string,
): void {
  const imagePath = resolveImagePath(node.image_path || "", astDir);
  if (!imagePath) {
    slide.addShape("rect", {
      x: box.x,
      y: box.y,
      w: box.w,
      h: box.h,
      fill: { color: "F2F2F2" },
      line: { color: "D0D0D0", width: 0.75, dash: "dash" },
    });
    return;
  }
  slide.addImage({ path: imagePath, x: box.x, y: box.y, w: box.w, h: box.h });
}

function renderLine(
  slide: any,
  pptx: any,
  node: ElementNode,
  box: { x: number; y: number; w: number; h: number },
): void {
  const style = node.style || {};
  slide.addShape(pptx.ShapeType.line, {
    x: box.x,
    y: box.y,
    w: box.w,
    h: box.h,
    line: {
      color: normalizeHex(style.stroke) || "666666",
      width: Number(style.strokeWidth || 1),
    },
  });
}

function mapBox(
  bbox: [number, number, number, number],
  context: { imageWidth: number; imageHeight: number; slideWidth: number; slideHeight: number },
): { x: number; y: number; w: number; h: number } {
  const [x, y, w, h] = bbox.map(Number) as [number, number, number, number];
  const looksLikePixels = context.imageWidth > context.slideWidth * 4 && context.imageHeight > context.slideHeight * 4;
  if (!looksLikePixels) {
    return { x, y, w, h };
  }
  return {
    x: round(x / context.imageWidth * context.slideWidth),
    y: round(y / context.imageHeight * context.slideHeight),
    w: round(w / context.imageWidth * context.slideWidth),
    h: round(h / context.imageHeight * context.slideHeight),
  };
}

function resolveImagePath(imagePath: string, astDir: string): string | null {
  if (!imagePath) {
    return null;
  }
  const candidates = [
    path.isAbsolute(imagePath) ? imagePath : "",
    path.resolve(astDir, imagePath),
    path.resolve(process.cwd(), imagePath),
  ].filter(Boolean);
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }
  return null;
}

function transparentFill(): { color: string; transparency: number } {
  return { color: "FFFFFF", transparency: 100 };
}

function opacityToTransparency(opacity?: number | null): number {
  if (opacity === undefined || opacity === null) {
    return 0;
  }
  if (opacity <= 1) {
    return Math.max(0, Math.min(100, Math.round((1 - opacity) * 100)));
  }
  return Math.max(0, Math.min(100, Math.round(100 - opacity)));
}

function normalizeHex(value?: string | null): string | null {
  if (!value) {
    return null;
  }
  const cleaned = value.trim().replace(/^#/, "");
  if (/^[0-9a-fA-F]{6}$/.test(cleaned)) {
    return cleaned.toUpperCase();
  }
  return null;
}

function round(value: number): number {
  return Math.round(value * 1000) / 1000;
}

function parseArgs(values: string[]): Args {
  const args: Partial<Args> = {};
  for (let index = 0; index < values.length; index += 1) {
    const value = values[index];
    if (value === "--input" || value === "-i") {
      args.input = values[index + 1];
      index += 1;
    } else if (value === "--output" || value === "-o") {
      args.output = values[index + 1];
      index += 1;
    }
  }
  if (!args.input || !args.output) {
    console.error("Usage: tsx backend/compiler/ppt_compiler.ts --input ast.json --output out.pptx");
    process.exit(2);
  }
  return args as Args;
}
