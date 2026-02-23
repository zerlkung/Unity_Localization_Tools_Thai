[> for Korean version of README.md](README.md)

# Unity Font Replacer

A tool to replace Unity game fonts with Korean/custom fonts. Supports both TTF and TextMeshPro SDF fonts.

## Quick Start (EXE-first)

After extracting a release ZIP, the folder typically looks like this:

```
release_en/
├── unity_font_replacer_en.exe
├── export_fonts_en.exe
├── KR_ASSETS/
├── Il2CppDumper/
└── README_EN.md
```

Recommended run:

```bat
cd release_en
unity_font_replacer_en.exe
```

Executables:

- `unity_font_replacer.exe`: Font replacement tool (Korean UI)
- `unity_font_replacer_en.exe`: Font replacement tool (English UI)
- `export_fonts.exe`: TMP SDF font exporter (Korean UI)
- `export_fonts_en.exe`: TMP SDF font exporter (English UI)

## Font Replacement (unity_font_replacer_en.exe)

### Basic Usage

```bat
:: Interactive mode (asks for game path)
unity_font_replacer_en.exe

:: Set game path + bulk replace with Mulmaru
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --mulmaru
```

### Command Line Options

| Option | Description |
|------|------|
| `--gamepath <path>` | Game root path or `_Data` folder path |
| `--parse` | Export font info to JSON (file-level worker scan to isolate crashes) |
| `--mulmaru` | Bulk replace all fonts with Mulmaru |
| `--nanumgothic` | Bulk replace all fonts with NanumGothic |
| `--sdfonly` | Replace SDF fonts only |
| `--ttfonly` | Replace TTF fonts only |
| `--list <JSON>` | Replace fonts from a JSON mapping |
| `--target-file <name>` | Limit replacement targets to specific file name(s) (repeatable/comma-separated) |
| `--use-game-material` | Keep original in-game Material parameters for SDF replacement (box artifacts may appear with Raster inputs) |
| `--use-game-line-metrics` | Keep in-game line metrics (LineHeight/Ascender/Descender, etc.) for SDF replacement (pointSize still follows replacement font) |
| `--original-compress` | Prefer original compression mode on save (default: uncompressed-family first) |
| `--temp-dir <path>` | Set root path for temporary save files (fast SSD/NVMe recommended) |
| `--output-only <path>` | Keep originals untouched and write modified files only to this folder (preserve relative paths) |
| `--preview` | With `--ps5-swizzle`, save unswizzled atlas preview + glyph crop PNGs into script-local `preview/` |
| `--scan-jobs <N>` | Set number of parallel scan workers (default: `1`, used by `--parse`/bulk scan paths) |
| `--ps5-swizzle` | Enable PS5 atlas swizzle detect/transform mode (`mask_x=0x385F0`, `mask_y=0x07A0F`, `rotate=90`) |
| `--split-save-force` | Skip one-shot and force one-by-one SDF split save for large multi-SDF replacements |
| `--oneshot-save-force` | Force one-shot only (disable split-save fallback) even for large multi-SDF replacements |
| `--verbose` | Save full logs to `verbose.txt` |

### Examples

```bat
:: Export font info (creates Muck.json)
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --parse

:: Export font info with parallel workers + PS5 swizzle detection fields
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --parse --scan-jobs 10 --ps5-swizzle

:: Replace all fonts with Mulmaru
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --mulmaru

:: Replace SDF only with NanumGothic
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --nanumgothic --sdfonly

:: Replace SDF and keep original in-game material parameters
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --nanumgothic --use-game-material

:: Keep in-game line metrics for SDF (pointSize still follows replacement font)
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --nanumgothic --use-game-line-metrics

:: Limit replacement to a specific file
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --nanumgothic --target-file "sharedassets0.assets"

:: Keep originals and write modified files only to a separate folder (preserve relative paths)
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --nanumgothic --output-only "D:\tevi"

:: Generate PS5 unswizzle previews (for original/replaced comparison)
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --list Muck.json --target-file "sharedassets0.assets" --ps5-swizzle --preview --sdfonly

:: Prefer original compression on save
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --nanumgothic --original-compress

:: Use a fast SSD/NVMe path for temporary save files
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --nanumgothic --temp-dir "E:\UFR_TEMP"

:: Skip one-shot and force one-by-one SDF split-save
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --nanumgothic --split-save-force

:: Disable split-save fallback and force one-shot only
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --nanumgothic --oneshot-save-force

:: Replace using JSON mapping
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --list Muck.json
```

## Per-Font Replacement (--list)

1. Run `--parse` to generate font info JSON.
2. Fill `Replace_to` for entries you want to replace.
3. Run with `--list`.

JSON example (without `--ps5-swizzle`):

```json
{
    "sharedassets0.assets|sharedassets0.assets|Arial|TTF|123": {
        "File": "sharedassets0.assets",
        "assets_name": "sharedassets0.assets",
        "Path_ID": 123,
        "Type": "TTF",
        "Name": "Arial",
        "Replace_to": "Mulmaru"
    },
    "sharedassets0.assets|sharedassets0.assets|Arial SDF|SDF|456": {
        "File": "sharedassets0.assets",
        "assets_name": "sharedassets0.assets",
        "Path_ID": 456,
        "Type": "SDF",
        "Name": "Arial SDF",
        "Replace_to": ""
    }
}
```

- If you run `--parse` with `--ps5-swizzle`, SDF entries include two additional fields:
  - `swizzle`: auto-detected target atlas state (`"True"`/`"False"`)
  - `process_swizzle`: whether to force replacement atlas into swizzled state (default `"False"`)
- `swizzle` and `process_swizzle` are inserted into JSON only when `--ps5-swizzle` is enabled.
- JSON loading uses `.get("swizzle")` / `.get("process_swizzle")`, so old JSON files (without these keys) remain compatible.

JSON example (with `--ps5-swizzle`, SDF):

```json
{
    "sharedassets0.assets|sharedassets0.assets|Arial SDF|SDF|456": {
        "File": "sharedassets0.assets",
        "assets_name": "sharedassets0.assets",
        "Path_ID": 456,
        "Type": "SDF",
        "Name": "Arial SDF",
        "swizzle": "True",
        "process_swizzle": "False",
        "Replace_to": ""
    }
}
```

- If `Replace_to` is empty, that font is skipped.
- Valid `Replace_to` forms:
  - `Mulmaru` or `Mulmaru.ttf`
  - `NanumGothic` or `NanumGothic.ttf`
  - `Mulmaru SDF` or `Mulmaru SDF.json` or `Mulmaru SDF Atlas.png`
  - `Mulmaru Raster` or `Mulmaru Raster.json` or `Mulmaru Raster Atlas.png`
  - `NGothic` or `NGothic.json` or `NGothic Atlas.png` or `NGothic Material.json`

## PS5 Validation Workflow (--preview)

To compare original vs replaced with the exact same extraction pipeline:

1. Create JSON for the target file only.
2. Run `--list + --ps5-swizzle + --preview` on original data (extract original crops).
3. Replace with `--nanumgothic --ps5-swizzle`.
4. Run `--list + --ps5-swizzle + --preview` again (extract replaced crops).
5. Compare the two preview outputs.

Example (single PS5 bundle target):

```bat
:: 1) Create JSON for a single target file
unity_font_replacer_en.exe --gamepath "C:\Game\Game_Data" --parse --target-file "38871756d6e98b9e67fb2e7a61dbb88e.bundle" --ps5-swizzle

:: 2) Extract original crops
unity_font_replacer_en.exe --gamepath "C:\Game\Game_Data" --list "Game.json" --target-file "38871756d6e98b9e67fb2e7a61dbb88e.bundle" --ps5-swizzle --preview --sdfonly

:: 3) Replace with NanumGothic (use --output-only to keep originals intact)
unity_font_replacer_en.exe --gamepath "C:\Game\Game_Data" --nanumgothic --sdfonly --target-file "38871756d6e98b9e67fb2e7a61dbb88e.bundle" --ps5-swizzle --output-only "D:\tevi"

:: 4) Extract replaced crops
unity_font_replacer_en.exe --gamepath "C:\Game\Game_Data" --list "Game.json" --target-file "38871756d6e98b9e67fb2e7a61dbb88e.bundle" --ps5-swizzle --preview --sdfonly
```

Output locations:
- Atlas preview: `preview\<file>\<assets_name>__<atlas_pathid>__<font>__unswizzled__*.png`
- Glyph crops: `preview\<file>\<assets_name>__<atlas_pathid>__<font>\U+XXXX*.png`

## Font Export (export_fonts_en.exe)

Exports TMP SDF font assets.

```bat
:: Positional path argument (recommended)
export_fonts_en.exe "D:\MyGame"

:: You can also pass _Data directly
export_fonts_en.exe "D:\MyGame\MyGame_Data"

:: If omitted, it prompts for the game path
export_fonts_en.exe
```

Output files are created in the current working directory:

- `TMP_FontAssetName.json`
- `TMP_FontAssetName SDF Atlas.png`
- (if present) `Material_*.json`

## Supported Fonts

| Font | Description |
|-----------|------|
| Mulmaru | Mulmaru Korean font |
| NanumGothic | NanumGothic Korean font |

## Adding Custom Fonts

Add these files under `KR_ASSETS`:

- `FontName.ttf` (required)
- `FontName.otf` (optional, can replace `.ttf`)
- `FontName SDF.json` or `FontName Raster.json` or `FontName.json` (optional, required for SDF replacement)
- `FontName SDF Atlas.png` or `FontName Raster Atlas.png` or `FontName Atlas.png` (optional, required for SDF replacement)
- `FontName SDF Material.json` or `FontName Raster Material.json` or `FontName Material.json` (optional)

If SDF data is missing, generate it first with `make_sdf.py` below or extract it with `export_fonts_en.exe`.

## SDF Generator (make_sdf.py)

You can generate TMP-compatible JSON/atlas directly from a TTF:

```bash
python make_sdf.py --ttf Mulmaru.ttf
```

Supported options:

| Option | Description | Default |
|--------|-------------|---------|
| `--ttf <ttfname>` | TTF file path/name | (required) |
| `--atlas-size <w,h>` | Atlas resolution | `4096,4096` |
| `--point-size <int or auto>` | Sampling point size | `auto` |
| `--padding <int>` | Atlas padding | `7` |
| `--charset <txtpath or characters>` | Charset file path or literal characters | `./CharList_3911.txt` |
| `--rendermode <sdf,raster>` | Output render mode | `sdf` |

## Run from Source (Optional)

If you prefer Python scripts instead of EXEs:

### Requirements

- Python 3.12 recommended
- Packages: `UnityPy (fork)`, `TypeTreeGeneratorAPI`, `Pillow`, `numpy`, `scipy`

```bash
pip install TypeTreeGeneratorAPI Pillow numpy scipy
pip install --upgrade git+https://github.com/snowyegret23/UnityPy.git
```

### Examples

```bash
python unity_font_replacer_en.py --gamepath "D:\Games\Muck" --mulmaru
python export_fonts_en.py "D:\MyGame"
```

## Notes

- Default save order prefers uncompressed-family modes (`safe-none -> legacy-none`), then falls back to `original -> lz4`.
- Use `--original-compress` to prefer original compression mode first.
- If save is slow, try `--temp-dir` and point it to a fast SSD/NVMe path.
- Interactive path input strips repeated wrapping quotes automatically.
- `--parse` scans via per-file worker processes so a crash in one file does not terminate the whole scan.
- You can increase scan throughput with `--scan-jobs`.
- Scanning uses blacklist-based exclusion (`*.bak`, `.info`, `.config`, etc.).
- `--output-only` keeps original files untouched and writes modified files only to the specified directory (preserving relative paths).
- `--preview` with `--ps5-swizzle` writes unswizzled atlas previews and glyph crops into `preview/`.
- With `--ps5-swizzle`, the tool uses metadata-first detection (with raw-data fallback) to decide target SDF atlas swizzle state and swizzles/unswizzles replacement atlases when needed.
- `swizzle` and `process_swizzle` are added to `--parse` JSON only when `--ps5-swizzle` is used.
- Set `process_swizzle: "True"` in JSON to force replacement atlas swizzle conversion regardless of auto detection.
- For large multi-SDF replacements, split-save fallback is enabled by default when one-shot fails (adaptive batch size).
  - `--split-save-force`: skip one-shot and force one-by-one SDF split-save.
  - `--oneshot-save-force`: disable split-save fallback and try one-shot only.
- Use `--target-file` to restrict replacements to specific files.
- If line spacing looks too tight or overlapping, try `--use-game-line-metrics`.
  This option still keeps pointSize from the replacement font.
- For SDF replacement, default behavior applies material floats from `KR_ASSETS/* SDF Material.json`.
  Use `--use-game-material` to preserve original in-game material style.
- When a Raster asset is injected into an SDF slot, SDF material effect floats (outline/underlay/glow) are automatically neutralized to reduce box artifacts.
- `TypeTreeGeneratorAPI` is required for TMP(FontAsset) parsing/replacement.
- Back up game files before modification.
- Some games may restore modified files by integrity checks.
- Check Terms of Service before using in online games.

## Special Thanks

- [UnityPy](https://github.com/K0lb3/UnityPy) by K0lb3
- [Il2CppDumper](https://github.com/Perfare/Il2CppDumper) by Perfare
- [NanumGothic](https://hangeul.naver.com/font) by NAVER | [License](https://help.naver.com/service/30016/contents/18088?osType=PC&lang=ko)
- [Mulmaru](https://github.com/mushsooni/mulmaru) by mushsooni | [License](https://github.com/mushsooni/mulmaru/blob/main/LICENSE_ko)

## License

MIT License
