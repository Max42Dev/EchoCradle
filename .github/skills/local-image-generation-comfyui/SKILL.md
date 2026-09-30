---
name: local-image-generation-comfyui
description: 'Generate game art (character portraits, textures, concept art, UI icons, item art) locally with ComfyUI via its MCP server. Use when creating or iterating on images, building ComfyUI workflows, or wiring generated art into the Unity asset pipeline. Triggers: ComfyUI, Stable Diffusion, generate image, texture, portrait, concept art, sprite, icon, workflow JSON, img2img, inpainting.'
---

# Local Image Generation (ComfyUI)

## When to Use
- Character portraits, NPC art, item icons, UI elements
- Seamless textures (stone, wood, fabric) for materials
- Concept art and mood boards
- Iterating on an image with img2img / inpainting

## Prerequisites
- ComfyUI installed and running (`python main.py --port 8188`).
- Checkpoint models in `ComfyUI/models/checkpoints/`.
- MCP server `comfyui` configured in `.vscode/mcp.json`.

## Procedure
1. **Start ComfyUI** and confirm it responds on `http://127.0.0.1:8188`.
2. **Pick or build a workflow** — start from a known-good text-to-image graph.
3. **Generate** with a descriptive prompt; keep resolution modest (512–1024) for iteration.
4. **Inspect** the result; refine with `regenerate` and parameter overrides.
5. **Upscale / clean up** only the keepers.
6. **Export** to `Assets/Art/Generated/<category>/` with descriptive names.
7. **Import into Unity** and set texture import settings (sprite vs texture, compression).

## Prompt Structure
`<subject>, <style>, <composition>, <lighting>, <quality tags>`
- Subject: "a grizzled dwarven blacksmith, full body"
- Style: "painterly fantasy, muted palette"
- Composition: "centered, plain background"
- Lighting: "soft rim light"
- Quality: "highly detailed, sharp focus"

Use **negative prompts** for artifacts: "blurry, extra fingers, watermark, text, lowres".

## Game-Art Recipes
| Asset | Approach |
|-------|----------|
| Character portrait | txt2img, square, consistent style prefix per faction |
| Seamless texture | txt2img + tiling workflow; verify seams |
| Item icon | txt2img on plain background → remove background → trim |
| Sprite sheet | generate frames → align → pack |
| Variation | img2img with low denoise to keep silhouette |

## Consistency Tips
- Reuse a fixed style prefix and seed per character/faction.
- Use a reference image + IP-Adapter / ControlNet for pose or silhouette control.
- Generate a small "style bible" set first, then match it.

## Unity Import Settings
- **Sprites:** Texture Type = Sprite (2D and UI), Pixels Per Unit matched to art scale.
- **Textures:** Texture Type = Default, sRGB on for albedo, off for masks/normal.
- **Compression:** use platform overrides; keep source PNGs in the repo.
- **Power-of-two** dimensions for mipmapping and compression.

## Pitfalls
- Generated art may have licensing/consistency issues — review before shipping.
- Large batches fill disk fast; clean up intermediates.
- Non-power-of-two textures compress poorly.
- Keep the workflow JSON in version control for reproducibility.

## References
- [ComfyUI](https://github.com/comfyanonymous/ComfyUI)
- [ComfyUI MCP server](https://github.com/joenorton/comfyui-mcp-server)
