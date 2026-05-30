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
  const isList = isUnorderedListText(text) || node.metadata?.role === "bullet";
  const fontSize = resolveFontSize(style, box, text);
  const bold = style.fontWeight === "bold" || Number(style.fontWeight) >= 600;
  const payload = buildTextPayload(text, fontSize);
  slide.addText(payload, {
    ...baseTextOptions(style, fontSize, bold, isList),
    x: box.x,
    y: box.y,
    w: box.w,
    h: box.h,
    valign: isList || text.includes("\n") ? "top" : "mid",
    align: isList ? "left" : style.align || "center",
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
  const isList = isUnorderedListText(textNode.text || "") || textNode.metadata?.role === "bullet";
  const fontSize = resolveFontSize(textStyle, box, textNode.text || "");
  const bold = textStyle.fontWeight === "bold" || Number(textStyle.fontWeight) >= 600;
  const payload = buildTextPayload(textNode.text || "", fontSize);
  slide.addText(payload, {
    ...baseTextOptions(textStyle, fontSize, bold, isList),
    x: box.x,
    y: box.y,
    w: box.w,
    h: box.h,
    shape: shapeType,
    valign: isList || (textNode.text || "").includes("\n") ? "top" : "mid",
    align: isList ? "left" : textStyle.align || "center",
    fill: fillColor ? { color: fillColor, transparency: opacityToTransparency(shapeStyle.opacity) } : transparentFill(),
    line:
      strokeColor && strokeWidth > 0
        ? { color: strokeColor, width: strokeWidth }
        : { color: "FFFFFF", transparency: 100, width: 0 },
  });
}

type RichTextRun = { text: string; options?: Record<string, unknown> };

function baseTextOptions(style: Style, fontSize: number, bold: boolean, isList: boolean): Record<string, unknown> {
  return {
    margin: 0,
    fit: "shrink",
    fontFace: style.fontFamily || "Aptos",
    fontSize,
    bold,
    color: normalizeHex(style.color) || "111111",
    breakLine: false,
    paraSpaceAfterPt: 0,
    paraSpaceAfter: 0,
    wrap: true,
    ...(isList ? { lineSpacingMultiple: 0.95 } : {}),
  };
}

function resolveFontSize(
  style: Style,
  box: { x: number; y: number; w: number; h: number },
  text: string,
): number {
  const styledSize = Number(style.fontSize || 0);
  if (styledSize > 0) {
    return round(Math.max(4, Math.min(72, styledSize)));
  }
  const lineCount = Math.max(1, text.split(/\r?\n/).filter((line) => line.trim()).length);
  const lineHeight = (box.h * 72) / lineCount;
  const ratio = lineCount === 1 ? 0.82 : 0.74;
  return round(Math.max(5, Math.min(60, lineHeight * ratio)));
}

function buildTextPayload(text: string, fontSize: number): string | RichTextRun[] {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const needsRichText = isUnorderedListText(text) || containsFormulaSyntax(text);
  if (!needsRichText) {
    return text;
  }

  const runs: RichTextRun[] = [];
  lines.forEach((line, lineIndex) => {
    const parsedList = parseUnorderedListLine(line);
    const content = parsedList ? parsedList.content : line;
    const lineRuns = parseFormulaRuns(content, fontSize);

    lineRuns.forEach((run, runIndex) => {
      const options = { ...(run.options || {}) };
      if (parsedList && runIndex === 0) {
        options.bullet = {
          characterCode: bulletCharacterCode(parsedList.marker),
          indent: bulletIndent(fontSize),
        };
        options.indentLevel = parsedList.level;
      }
      if (lineIndex < lines.length - 1 && runIndex === lineRuns.length - 1) {
        options.breakLine = true;
      }
      runs.push({ text: run.text, options });
    });

    if (lineRuns.length === 0 && lineIndex < lines.length - 1) {
      runs.push({ text: "", options: { breakLine: true } });
    }
  });
  return runs;
}

function parseFormulaRuns(text: string, fontSize: number): RichTextRun[] {
  const runs: RichTextRun[] = [];
  let buffer = "";
  let index = 0;
  let foundScript = false;

  const flush = () => {
    if (buffer) {
      runs.push({ text: buffer });
      buffer = "";
    }
  };

  while (index < text.length) {
    const char = text[index];
    if ((char === "_" || char === "^") && index + 1 < text.length && isScriptStart(text[index + 1])) {
      const script = readScriptText(text, index + 1);
      if (script.text) {
        flush();
        runs.push({
          text: script.text,
          options: {
            [char === "_" ? "subscript" : "superscript"]: true,
            fontSize: round(Math.max(4, fontSize * 0.72)),
          },
        });
        foundScript = true;
        index = script.nextIndex;
        continue;
      }
    }
    buffer += char;
    index += 1;
  }
  flush();
  return foundScript ? runs : [{ text }];
}

function readScriptText(text: string, startIndex: number): { text: string; nextIndex: number } {
  if (text[startIndex] === "{") {
    const closeIndex = text.indexOf("}", startIndex + 1);
    if (closeIndex > startIndex + 1) {
      return { text: text.slice(startIndex + 1, closeIndex), nextIndex: closeIndex + 1 };
    }
  }
  const match = text.slice(startIndex).match(/^[A-Za-z0-9+\-=(),\u0370-\u03ff]+/);
  if (match?.[0]) {
    return { text: match[0], nextIndex: startIndex + match[0].length };
  }
  return { text: text[startIndex] || "", nextIndex: startIndex + 1 };
}

function containsFormulaSyntax(text: string): boolean {
  return /[_^](?:\{[^}]+\}|[A-Za-z0-9\u0370-\u03ff])/.test(text);
}

function isScriptStart(value: string): boolean {
  return value === "{" || /[A-Za-z0-9\u0370-\u03ff]/.test(value);
}

function isUnorderedListText(text: string): boolean {
  return text.split(/\r?\n/).some((line) => parseUnorderedListLine(line) !== null);
}

function parseUnorderedListLine(line: string): { marker: string; content: string; level: number } | null {
  const match = line.match(/^(\s*)([•●◦○▪■□‣⁃\-–—])\s+(.+)$/u);
  if (!match) {
    return null;
  }
  return {
    marker: match[2],
    content: match[3],
    level: Math.max(0, Math.min(6, Math.floor(match[1].replace(/\t/g, "  ").length / 2))),
  };
}

function bulletCharacterCode(marker: string): string {
  const codeByMarker: Record<string, string> = {
    "•": "2022",
    "●": "25CF",
    "◦": "25E6",
    "○": "25CB",
    "▪": "25AA",
    "■": "25A0",
    "□": "25A1",
    "‣": "2023",
    "⁃": "2043",
    "-": "2013",
    "–": "2013",
    "—": "2014",
  };
  return codeByMarker[marker] || "2022";
}

function bulletIndent(fontSize: number): number {
  return round(Math.max(9, Math.min(24, fontSize * 0.9)));
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
  if (context.imageWidth <= 0 || context.imageHeight <= 0) {
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
