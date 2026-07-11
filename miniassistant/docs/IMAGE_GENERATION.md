# Image Generation & Editing

When the user asks to generate/create an image, use the configured image generation model.
When the user uploads an image and asks to **edit/change/modify** it, use Image Editing (img2img).

## Config

```yaml
image_generation:
  - "google/gemini-2.5-flash-image"       # Google Gemini (native image generation)
  - "openai/dall-e-3"                      # OpenAI DALL-E 3
  - "openai/chatgpt-image-latest"          # OpenAI ChatGPT Image
  # - "ollama-online/flux"                 # Can also use Ollama models with provider prefix
```

Image generation is a **list** of models. Use `invoke_model(model='EXACT_NAME_FROM_LIST', message='PROMPT')` to generate.
If the list is empty: tell the user "Kein Bildgenerierungs-Modell konfiguriert. Bitte `image_generation` in der Config setzen."

## Workflow — Image Generation (new image from text)

1. **Generate the image** using `invoke_model` with one of the configured models (use the **exact name** including provider prefix).
   The system automatically saves generated images to `WORKSPACE/images/` (the configured workspace directory).
2. **Send the image** using the `send_image` tool:
   ```
   send_image(image_path="/absolute/path/to/image.png", caption="Beschreibung")
   ```
   The tool automatically detects the current platform (Matrix/Discord/Web-UI) and uploads the image:
   - **Matrix:** Uploads to media repo via bot client (E2EE-capable), sends as `m.image`.
   - **Discord:** Uploads as file attachment via bot API.
   - **Web-UI:** Returns the file path (inline display not supported).

**No curl commands needed.** The `send_image` tool handles credentials, room/channel detection, and upload automatically.

## Workflow — Image Editing (modify an existing image)

When the user uploads an image and asks to edit/modify/transform it, the image is automatically saved to disk.
You will see a `[Hochgeladenes Bild gespeichert unter:]` block with the file path(s).

**Use that path with `image_path`:**
```
invoke_model(model='EXACT_NAME_FROM_LIST', message='make the sky sunset orange', image_path='/path/to/uploaded/image.png')
```

**Optional `strength` parameter** (0.0–1.0): Controls how much the image changes.
- `0.1–0.3` = subtle changes (color correction, minor touch-ups)
- `0.4–0.6` = moderate changes (style transfer, object modifications)
- `0.7–1.0` = major changes (heavy transformation, almost new image)
- **System default when omitted on edit calls: `1.0`** (configurable via `image_edit_strength`, per model/provider/global) — distill edit models (`flux-klein`, `qwen-image-edit`) behave ~bimodal: values below 1.0 often return the input nearly unchanged. Pass an explicit lower value only when the user asks for subtle changes.

**Edit model auto-selection:** when `image_path` is set and `model` is not specified, the system uses the FIRST model in the `image_generation:` config list (user controls ordering). Explicit `model='…'` is always honored.

**Group-room path translation:** in group rooms, `image_path` must be a sandbox-relative path (`/workspace/...`). The system translates it to the host path under `<workspace>/groups/<sub>/` automatically. Refuses paths outside the room workspace.

## Parameter synonyms — what the user means

Users often use informal or German words for technical parameters. Map them correctly:

| User says | Parameter | Example |
|-----------|-----------|---------|
| "10 Durchläufe", "10 Schritte", "10 steps", "10 iterations" | `steps=10` | `invoke_model(..., steps=10)` |
| "1024x900", "in 1024x900", "Größe 1024x900" | `size="1024x900"` | `invoke_model(..., size='1024x900')` |
| "CFG 7", "Guidance 7", "CFG-Scale 7" | `cfg_scale=7` | `invoke_model(..., cfg_scale=7)` |
| "Seed 42", "gleicher Seed" | `seed=42` | `invoke_model(..., seed=42)` |
| "Stärke 0.5", "strength 0.5", "leicht verändern" | `strength=0.5` | `invoke_model(..., strength=0.5)` |
| "ohne Hintergrund", "kein Text" | `negative_prompt="background, text"` | `invoke_model(..., negative_prompt='...')` |

**CRITICAL:** These are **parameters of a single `invoke_model` call** — NOT separate calls. "10 Durchläufe" means `steps=10` in ONE call, NOT 10 separate image generations.

### How to decide: Generate vs Edit

| User says | Action |
|-----------|--------|
| "erstelle ein Bild von..." / "generate..." | **Generate** — no `image_path` |
| "ändere das Bild..." / "mach den Himmel rot" (+ uploaded image) | **Edit** — use `image_path` |
| "mach das Bild schärfer" / "entferne den Hintergrund" (+ uploaded image) | **Edit** — use `image_path` |
| "erstelle ein ähnliches Bild wie dieses" (+ uploaded image) | **Edit** with high strength (~0.8) |
| Bild hochgeladen ohne Text / "was ist das?" | **Vision/Analyse** — do NOT edit, just describe |

## Ideogram-class models — JSON prompts & safety filter

Ideogram 4 (and models listed in config `image_json_prompt_models`) has a safety filter **baked into the model weights**. It cannot be disabled. On plain-text prompts it false-positives constantly — even a harmless donut gets a gray "Image blocked by safety filter" placeholder instead of an image. The model is trained on **structured JSON captions**; JSON prompts drastically reduce false blocks and improve composition.

**What happens automatically:** plain-text prompts to these models are auto-wrapped into a minimal JSON caption, and returned placeholder images are detected and reported as an error instead of being sent to the user.

**For best quality**, pass the full JSON schema yourself as the `message`:

```json
{
  "high_level_description": "1-2 sentence scene summary.",
  "style_description": {
    "aesthetics": "mood keywords",
    "lighting": "lighting description",
    "medium": "photograph | 3D render | oil painting | ...",
    "art_style": "only for non-photos; for photos use key \"photo\" BEFORE medium instead",
    "color_palette": ["#RRGGBB", "..."]
  },
  "compositional_deconstruction": {
    "background": "environment description",
    "elements": [
      {"type": "obj", "bbox": [0, 0, 1000, 1000], "desc": "object description"},
      {"type": "text", "bbox": [100, 100, 300, 900], "text": "EXACT TEXT", "desc": "font/style"}
    ]
  }
}
```

Rules: `bbox` = `[y_min, x_min, y_max, x_max]` in 0–1000 normalized coordinates. `style_description` needs exactly one of `photo`/`art_style`. Key order matters (objects: type, bbox, desc, color_palette). Send the raw JSON as the message — no surrounding prose, code fences are tolerated.

**If a generation is still blocked:** rephrase more neutrally (avoid brand names, celebrity/person names, violence-adjacent words) and retry once, or use another configured image model (e.g. flux/krea have no such filter). Inform the user if it stays blocked.

## CRITICAL — NEVER generate fake images

**NEVER** output `![...](data:image/png;base64,...)` or any base64-encoded image data in your response text. You CANNOT generate images by writing base64 — that produces garbage data, not a real image. **ALWAYS** use `invoke_model` with a configured image generation model, then `send_image` to deliver it. There is no shortcut.

## Always available

Image generation is **always available** when configured — no per-provider activation needed.

**Google Gemini:** Gemini 2.0/2.5 Flash and Imagen support native image generation and editing via the API. For editing, the source image is automatically included in the API request as inlineData.

**OpenAI DALL-E / ChatGPT Image:** DALL-E and chatgpt-image-latest support image generation via `POST /v1/images/generations` and editing via `POST /v1/images/edits`. Supports sizes: 1024x1024, 1024x1792, 1792x1024.

**Local backends (sd-server, LocalAI, Flux):** Image editing uses `/v1/images/edits` with the source image as base64. If the backend doesn't support `/edits`, it falls back to `/generations` with the image parameter. Parameters like `steps`, `cfg_scale`, `strength` are passed via `<sd_cpp_extra_args>` tags.
