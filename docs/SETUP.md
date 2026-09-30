# EchoCradle — Environment Setup

Complete guide to the local AI game-development environment. Everything runs
**locally** on Windows — no cloud APIs required.

## Hardware Baseline (this machine)

| Component | Spec |
|-----------|------|
| GPU | NVIDIA L4, 24 GB VRAM (CUDA 13.2, driver 596.86) |
| CPU | AMD EPYC 7R13 |
| RAM | 16 GB |
| Disk | C: ~80 GB total — **keep an eye on free space** |

> **Disk is the main constraint.** Unity Editor (~10 GB), ComfyUI + models
> (5–20 GB), and Ollama models (4–9 GB each) add up fast. Install only what you
> need and clean up intermediates.

## Installed Tooling

| Tool | Version | Location |
|------|---------|----------|
| Unity Hub | latest | `C:\Program Files\Unity Hub` |
| Unity Editor | 6000.0.84f1 LTS (installed, registered) | `C:\Program Files\Unity\Hub\Editor\6000.0.84f1` |
| Blender | 5.2.1 LTS | `C:\Program Files\Blender Foundation\Blender 5.2` |
| Python | 3.13 | `C:\Program Files\Python313` |
| uv / uvx | 0.12.x | `C:\Users\Administrator\.local\bin` |
| Node.js | v24.21.0 LTS | `C:\tools\node` |
| Ollama | 0.34.x | `%LOCALAPPDATA%\Programs\Ollama` |
| .NET SDK | 10.0 | system |
| Git | 2.55 | system |

Both `C:\tools\node` and `C:\Users\Administrator\.local\bin` are on the machine
`PATH`. **Restart VS Code** after first setup so it picks up the new PATH.

---

## 1. Unity

### Account
Unity Hub requires a **free Unity ID** to download editors and activate a
license. Sign in once via the Hub GUI and activate a **Unity Personal** license
(free). The license is cached locally, so later headless/batch-mode runs work
without interaction.

### Install the editor
```powershell
& "C:\Program Files\Unity Hub\Unity Hub.exe" -- --headless install --version 6000.0.84f1
```

> **Windows Server note.** On Windows Server 2022 the Hub's silent install can
> fail with `INSTALL_ERROR` / exit code `2`. This is caused by an unusual
> `%TEMP%` path (e.g. `...\Temp\1`). Workaround — run the downloaded installer
> directly with a clean TEMP:
> ```powershell
> $inst = "$env:APPDATA\UnityHub\downloads\UnitySetup64-6000.0.84f1.exe"
> New-Item -ItemType Directory -Force -Path C:\tools\utemp | Out-Null
> $env:TEMP = "C:\tools\utemp"; $env:TMP = "C:\tools\utemp"
> Start-Process $inst -ArgumentList "/S","/D=C:\Program Files\Unity\Hub\Editor\6000.0.84f1" -Wait
> ```
> Then register it with the Hub:
> ```powershell
> & "C:\Program Files\Unity Hub\Unity Hub.exe" -- --headless editors --add "C:\Program Files\Unity\Hub\Editor\6000.0.84f1\Editor\Unity.exe"
> ```

Add build modules later if needed:
```powershell
& "C:\Program Files\Unity Hub\Unity Hub.exe" -- --headless install-modules --version 6000.0.84f1 -m windows-il2cpp
```

### Create the project
1. Open Unity Hub → **New project** → **3D (URP)** → name it `EchoCradle` →
   location `C:\projects\EchoCradle\Unity` (keep the Unity project in a subfolder
   so it doesn't collide with the repo root).
2. Open the project.

### Install the Unity MCP plugin
In Unity: **Window → Package Manager → + → Add package from git URL**:
```
https://github.com/CoplayDev/unity-mcp.git?path=/MCPForUnity#main
```
Then **Window → MCP for Unity → Configure All Detected Clients**.

> Alternative: the IvanMurzak plugin (`com.ivanmurzak.unity.mcp`) via OpenUPM,
> which also generates skills. Either works; the config in `.vscode/mcp.json`
> targets the CoplayDev server (`mcpforunityserver`).

---

## 2. Blender

Blender 5.2.1 is installed. The MCP addon is already copied to:
```
%APPDATA%\Blender Foundation\Blender\5.2\scripts\addons\blender_mcp.py
```

### Enable it
1. Open Blender → **Edit → Preferences → Add-ons**.
2. Search **"MCP for Blender"** and enable it.
3. In the 3D viewport press `N` → **MCP for Blender** tab → **Start MCP Server**.

The server listens on `localhost:9876` (matches `.vscode/mcp.json`).

To reinstall/update the addon:
```powershell
$env:BLENDERMCP_ADDONS_DIR = "$env:APPDATA\Blender Foundation\Blender\5.2\scripts\addons"
uvx --python 3.11 mcp-for-blender install-addon
```

---

## 3. Ollama (local LLM)

Ollama is installed and running as a service on `http://127.0.0.1:11434`.

### Pull models
```powershell
ollama pull llama3.1:8b          # general text / lore / dialogue
ollama pull qwen2.5-coder:7b     # C# / code generation
ollama pull nomic-embed-text     # embeddings for lore RAG
```
Optional (needs more VRAM, better reasoning):
```powershell
ollama pull qwen2.5:14b
```

### Verify
```powershell
ollama list
ollama run llama3.1:8b "Write one sentence of dark fantasy lore."
```

---

## 4. ComfyUI (local image generation)

ComfyUI is **not installed by default** (disk constraint). Install it when you
need image generation.

### Install
```powershell
cd C:\tools
git clone https://github.com/comfyanonymous/ComfyUI.git
cd ComfyUI
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
pip install -r requirements.txt
```

### Get a model
Download a checkpoint (e.g. SD 1.5 or SDXL-Turbo) into
`ComfyUI\models\checkpoints\`. SD 1.5 (~4 GB) is the lightest option.

### Run
```powershell
python main.py --port 8188
```
The MCP server connects to `http://127.0.0.1:8188`.

---

## 5. MCP Servers

All configured in [`.vscode/mcp.json`](../.vscode/mcp.json). They are launched
on demand by VS Code via `uvx`/`npx`.

| Server | Launch | Needs running |
|--------|--------|---------------|
| `blender` | `uvx mcp-for-blender` | Blender + addon server |
| `unity` | `uvx mcpforunityserver` | Unity Editor + plugin |
| `comfyui` | `uvx mcp-comfyui` | ComfyUI on :8188 |
| `ollama` | `uvx mcp-ollama` | Ollama on :11434 |
| `filesystem` | `npx @modelcontextprotocol/server-filesystem` | — |
| `git` | `uvx mcp-server-git` | — |
| `memory` | `npx @modelcontextprotocol/server-memory` | — |
| `sequential-thinking` | `npx @modelcontextprotocol/server-sequential-thinking` | — |
| `fetch` | `uvx mcp-server-fetch` | — |

> **Note:** `mcp-ollama` and `mcp-comfyui` are pinned to `mcp<2` because they
> still use the v1 `FastMCP` API. If they fail to start, that pin is the reason.

### Enable in VS Code
1. Open the Command Palette → **MCP: List Servers**.
2. Start the servers you need. Trust the workspace when prompted.

---

## 6. Verify the Whole Stack

```powershell
# Tooling
uvx --version; node --version; ollama --version

# Blender MCP server starts
uvx --python 3.11 mcp-for-blender --help

# Unity MCP server starts
uvx --from mcpforunityserver mcp-for-unity --help

# Ollama responds
ollama list
```

Then in VS Code chat, ask: *"List the MCP tools available."* You should see
tools from the servers you started.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Unity Hub install fails (exit code 2) | Run the installer directly with a clean `%TEMP%` (see above) |
| Unity Hub asks for an account | Sign in with a free Unity ID; activate Personal license |
| `uvx`/`node` not found in VS Code | Restart VS Code so it reloads PATH |
| Blender MCP "not connected" | Start the addon server in Blender (`N` → Start MCP Server) |
| Unity MCP "not connected" | Open the Unity project; check the plugin window |
| `mcp-ollama`/`mcp-comfyui` crash | Ensure `--with "mcp<2"` is in the args |
| ComfyUI connection refused | Start ComfyUI on port 8188 |
| Out of disk | Remove unused Ollama models (`ollama rm`) and ComfyUI outputs |
