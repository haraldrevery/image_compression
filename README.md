# minjpg

A small Tkinter app that automates both halves of the photo workflow, replacing
the manual Squoosh passes:

1. **Compress images** — anything off the camera or phone (HEIC, PNG, TIFF,
   WebP, RAW-adjacent formats…) becomes a web-ready sRGB JPEG capped at a long
   edge.
2. **Thumbnails** — those web originals become `*_min.jpg` thumbnails, every one
   guaranteed under the **70 KB** rule.

Both tabs work the same way: you name an **input folder** and an **output
folder**, and the app creates a **new folder inside the output folder** for that
run. Nothing is ever written to a folder that already held your files, so a run
cannot overwrite anything.

Instead of nudging sliders per image, the app searches: it fits the image to a
size cap, then finds the highest MozJPEG quality that lands inside the byte
budget.

## What it reproduces

The encoder is real MozJPEG (`cjpeg`, bundled), driven with the same settings
used in Squoosh:

| Squoosh option           | Here                                        |
| ------------------------ | ------------------------------------------- |
| MozJPEG, Channels YCbCr  | default for RGB input                       |
| Quantization: ImageMagick| `-quant-table 3`                            |
| Smoothing: 30            | `-smooth 30` (configurable)                 |
| Auto subsample chroma    | no `-sample` flag — MozJPEG decides (4:2:0) |
| Progressive, optimized   | `-progressive -optimize`                    |
| Resize: lanczos3, linearRGB | Lanczos3 filtering in linear light       |

Thumbnails carry no EXIF and no ICC profile, matching the existing `_min.jpg`
files. (The Compress images tab keeps metadata; see below.)

## Defaults

**Thumbnails** (Settings tab). Whatever the fields say is used at the next Scan
on the Thumbnails tab, which also saves it — there is no separate Apply step to
forget. **Reset to defaults** asks first.

| Setting            | Value  | Why                                                    |
| ------------------ | ------ | ------------------------------------------------------ |
| Max long edge      | 1280   | No short-edge cap; aspect ratio always preserved       |
| Size target        | 68,000 | The quality search aims at or below this               |
| Hard cap           | 71,680 | 70 KiB, never exceeded — nothing is written above it    |
| Quality floor      | 30     | Lowest quality the search will accept                  |
| Quality ceiling    | 75     | Squoosh's own default; easy images stay small          |
| Smoothing          | 30     |                                                        |

Images are never upscaled. If even the quality floor overshoots the target, the
image is shrunk in steps (at most 20% per round, at least 2%, down to a 480 px
long edge) and re-searched, because a smaller image at decent quality beats a
full-size one at quality 30.

**Compress images** (fields on its own tab):

| Setting        | Value   | Why                                                     |
| -------------- | ------- | ------------------------------------------------------- |
| Max long edge  | 3840    | Median long edge of the existing high-res originals     |
| Quality        | 65      | Their median quality                                    |
| Max size       | 600 KB  | Their p90 is 646 KB; set to 0 to let quality decide     |
| Quality floor  | 40      | How far the search drops to honour the cap (never above Quality) |
| Smoothing      | 0       | Smoothing pays off at thumbnail sizes, not at 3840 px   |

Here the size cap is a *preference*, not a rule: if even quality 40 cannot meet
it, the file is still written and the row is flagged **over cap** so you can
decide whether to lower the long edge yourself.

## Running from source

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py
```

Only Pillow and numpy are needed at runtime; PyInstaller is only for building.

## Where the results go

Both tabs need two folders, and neither has a default — the app never picks a
destination for you:

- **Input folder** — read only. Nothing in it is ever modified.
- **Output folder** — where the app creates a **new folder** for this run, named
  after the input folder, the job and the minute:

  ```
  Input folder:   /home/me/Pictures/photos
  Output folder:  /home/me/Pictures/out

  creates ->      /home/me/Pictures/out/photos_min_2026-08-27_1432/
  ```

The header always spells out the exact folder that will be created, before you
press anything. If a folder of that name is somehow already there, the app says
so and adds `_2`, `_3` and so on — it never writes into a folder that exists.

The output folder must already exist. If it does not, Scan asks before creating
it, naming it in full and reminding you to connect the drive first if it belongs
on one — so a typo, or a drive that is not plugged in, never turns into a folder
on this computer's own disk that quietly fills up.

The output folder cannot be the input folder or sit inside it; both are refused
with an explanation. It may contain the input — `~/Pictures` as the output for
`~/Pictures/photos` — because the run folder is always a new one beside it.

## Using the Thumbnails tab

1. Pick an **input folder** and an **output folder**.
2. Choose the layout on the **Settings** tab:

   **In their own `_min/` folder** (the default) — only thumbnails are written,
   in a tree mirroring the input:

   ```
   photos_min_2026-08-27_1432/
     _min/a_min.jpg
     _min/sub/c_min.jpg
   ```

   **Next to their originals** — the whole input tree is copied across, images
   and non-images alike, with each thumbnail beside its original. The new folder
   then stands on its own, and uses about as much disk space again as the input:

   ```
   photos_min_2026-08-27_1432/
     a.jpg          notes.txt
     a_min.jpg      sub/c.jpg
                    sub/c_min.jpg
   ```

3. The status line shows how many images need a `_min.jpg` and how many files
   will be copied. Start says exactly what is about to happen and asks first.
   HEIC and the other formats only the Compress tab reads are counted in the
   scan notes rather than passed over in silence: compress them first.
4. Click any row for an original vs. `_min.jpg` preview with dimensions and size.
5. Not happy with one? Type a long edge and/or quality under the preview and hit
   **Re-do selected**. A quality override skips the search entirely — it still
   refuses to write anything above the hard cap.

JPEGs whose name already ends in `_min` — or `_min-2`, `_min-3`…, the names a
clash produces — are never used as input, so re-running over a finished folder
does not compress the compressed. Only JPEGs count, and only clash numbers of up
to three digits, so `trip_min-2024.jpg` is still treated as a photo. In the
"next to their originals" layout they are still copied across, and if a generated
thumbnail wants a name an existing file already has, the **existing file keeps
its name** and the generated one becomes `-2`. Each row shows where its result
goes (`a.jpg → a_min.jpg`), so an image's copy and its thumbnail are told apart.

## Using the Compress images tab

1. Pick an **input folder** and an **output folder**.
2. Set the long edge, quality and optional max size, then **Scan** and **Start**.

The new folder is a full mirror of the input: every image replaced by its
compressed JPEG, every other file — videos, sidecars, notes — copied across
untouched, so nothing in the tree is lost on the way.

Sources may be JPEG, PNG, TIFF, WebP, BMP, GIF, PSD, JPEG2000, TGA and — with
`pillow-heif` installed — HEIC/HEIF and AVIF. A layered PSD uses its flattened
image. A multi-page TIFF, or an animated GIF, WebP or PNG, cannot become one JPEG
without losing frames, so it is copied across unchanged and its row says
**kept original**. (The Thumbnails tab still makes a thumbnail of the first frame.)

What it guarantees:

- **Everything comes out sRGB.** A Display P3 or Adobe RGB source is converted
  through its embedded profile rather than being reinterpreted, which would leave
  it dull and hue-shifted. CMYK and greyscale profiles are applied to the image in
  its own colour mode. A damaged profile, or one that cannot be applied, falls
  back to the raw pixels instead of failing the file — and the row says **check
  colours**, as it does for a CMYK image with no profile at all.
- **16-bit sources are scaled, not clipped.** A 16-bit greyscale scan becomes the
  same greys in 8 bits rather than solid white, and the row notes the reduction.
- **Nothing is silently left out.** An image that cannot be read — corrupt, too
  large for the decoder or for the memory available — is copied across unchanged
  instead, and its row says **kept original**. The same happens to multi-frame
  files, as above. A broken encoder is a different matter: those images fail and
  keep the folder marked incomplete, and three encoder failures in a row stop the
  run, rather than quietly filling the folder with unconverted originals.
- **File dates are kept.** Copies and compressed files carry their source's
  modification date; for anything without EXIF, that date is the only one there is.
- **Metadata is kept** unless you tick **Remove all metadata**: EXIF (camera,
  lens, date, exposure, GPS), XMP (captions, keywords, ratings, colour labels and
  develop settings from Lightroom, Bridge, Capture One, darktable and the like)
  and IPTC (the older caption and keyword block the same apps still write). A
  TIFF's camera data, date taken, GPS and Windows ratings and keywords come
  across too — but not the tags that describe the TIFF file itself — as does EXIF
  that ImageMagick stored in a PNG as text. File-manager tags — the tags, ratings
  and comments KDE Dolphin and similar keep beside a file on Linux — travel with
  every converted image and every copy. What the conversion changes is corrected
  in all of them: the orientation is reset to 1 because the rotation is baked
  into the pixels, dimensions are updated to the real output size, the colour
  space says sRGB after a conversion, and stale embedded thumbnails are dropped.
  A JPEG holds at most 64 KB per block, so XMP that does not fit first loses what
  nobody typed — Camera Raw develop settings, edit history, thumbnails — and
  keeps captions, keywords and ratings. Anything that still cannot be kept — a
  block too big for a JPEG, the overflow of a huge XMP packet, an IPTC profile
  stored as PNG text, tags a FAT or exFAT drive cannot hold — marks the row
  **metadata lost** and is named in the end-of-run dialog.
- **Subfolders are mirrored**, empty ones included, and if two sources map to
  the same name (`photo.png` and `photo.tif`) the second becomes `photo-2.jpg`
  rather than overwriting the first. A real JPEG always keeps its own name: with
  `IMG_1234.HEIC` and `IMG_1234.JPG` side by side, the JPEG stays `IMG_1234.jpg`
  and the converted HEIC becomes `IMG_1234-2.jpg`. A renamed output shows its new
  name on its row. The `._` files a Mac leaves on USB drives are copied as they
  are, never mistaken for photos.
- **A JPEG that already fits** both the long edge and the size cap is copied
  verbatim — no generation loss. That is skipped when metadata is being stripped
  or the source needs a colour conversion, since a plain copy would defeat both.
  It is judged by content, not name: a PNG or HEIC saved as `.jpg`, or a CMYK
  JPEG, is converted properly instead of being copied.

## Not losing work

Both tabs share the same guards, so neither can quietly destroy files:

- **Every run writes into a folder created for it alone.** That is the whole
  design: the destination did not exist a moment earlier, so there is nothing in
  it to overwrite. The folder is not created until you press Start and confirm —
  Scan only reserves the name.
- **An existing name is never reused.** If the generated name is already taken —
  by a folder, a file, or even a broken symlink — the app says so and adds a
  numeric suffix. It never writes into something that is already there.
- **The output cannot be, or sit inside, the input.** Both are refused with an
  explanation.
- **The output folder is never created unasked.** Scan offers to create a missing
  one; Start refuses if it has gone since the scan.
- **Results never land on their sources.** Any job whose output would resolve to
  its own input is refused and reported.
- **A run that writes nothing leaves nothing.** Cancel before the first file, or
  have every image fail, and the empty run folder is removed again. Only ever an
  empty one — a folder with anything in it is never cleaned up.
- **An unfinished folder says so.** `_minjpg_INCOMPLETE.txt` is written into the
  new folder before anything else and removed only once the run is complete.
  For a folder meant to mirror the input — the Compress tab, and thumbnails next
  to their originals — complete means the input has been walked again at the end
  and everything in it is accounted for. A file added during the run, a folder
  that could not be read, a linked folder (links are not followed), or subfolders
  left out with **Include subfolders** off all keep the marker; the Start dialog
  says so beforehand. Cancel, a failure, a crash, a power cut or closing the app
  all leave it there too. It lists the files that failed, anything missing from
  the input and whatever was never processed — except after a crash, which leaves
  it as written at the start. While it is there, do not delete originals on the
  strength of that folder. Re-doing a failed image successfully updates it; a
  re-do cannot clear items missing from the input.
- **Problems are said out loud.** A run where files failed, were kept as
  originals, lost metadata, are missing from the mirror, or stopped early ends
  with a dialog naming them, not just a line in the log. Folders that cannot be
  read, and linked folders, are listed before Start rather than turning up as
  empty folders in the mirror.
- **A full disk stops the run.** The first "no space left" ends the batch instead
  of failing every remaining file while the disk stays full, and names the file
  it could not write — which may be the encoder's temporary file on the system
  drive rather than anything in the output.
- **A drive that goes away stops the run.** If the run folder disappears mid-run
  — an unplugged drive, say — the run stops at the next file instead of carrying
  on in a folder re-created on the disk underneath.
- **Copied files are verified before they land.** Every copy is checked against
  its source's size before it takes its real name, so a copy cut short by a full
  disk fails without leaving a truncated file behind. Dates and file-manager tags
  come across; permissions do not, so a read-only original never produces an
  output that a later re-do cannot replace.
- **Name clashes are case-insensitive.** `photo.PNG` and `photo.png` become
  `photo.jpg` and `photo-2.jpg` rather than silently colliding on Windows or macOS.
- **Pre-flight before every batch.** The output folder is probed for writability
  before any work starts, and low free space is shown in the Start dialog itself —
  counting the bytes of a full-tree copy, not just the images.
- **Nothing is swept up.** Work in progress goes to a randomly named
  `.minjpg-….part` file created fresh in the new folder, so it can never land on a
  real file — not even one of yours called `photo.jpg.part`. Scanning never
  deletes anything; `.part` is also what browsers name downloads in progress.
- **Every write is atomic and durable.** Files are written (or copied) to that
  temp file, flushed to disk, and only then moved into place, so neither a crash
  nor a power cut leaves a truncated or empty file under a real name. The flush
  costs about a millisecond per file on an SSD — noticeable only when copying many
  thousands of small files to a slow USB stick.
- **Nothing changes mid-run.** While a batch is running, Scan, Browse, Re-do and
  Start decline rather than repoint the work under way, and the run keeps the
  settings its scan took: editing a field — on either tab or the Settings tab —
  changes nothing until the next Scan.
- **The encoder cannot hang the app.** `cjpeg` is given 120 seconds per image;
  past that it is stopped and only that one file is marked failed. Three encoder
  failures in a row stop the run, as every remaining image would fail the same way.
- **The bundled encoder is verified.** Its SHA-256 is checked against the
  recorded digest before it is ever executed.

Both tabs' settings are saved to `~/.config/minjpg/settings.json` (Linux) or
`%APPDATA%\minjpg\settings.json` (Windows). If that file is unreadable or holds
a value that fails validation, the affected tab says so in its log and falls
back to defaults rather than silently discarding your settings.

## When something goes wrong

The app is built windowed, so there is no console to print to. Everything in the
in-app log, plus any crash, is written to `minjpg.log` next to `settings.json`
(`~/.config/minjpg/minjpg.log` on Linux, `%APPDATA%\minjpg\minjpg.log` on
Windows), rotating at 1 MB with three kept. That file is what to send with a bug
report.

To run the two steps in sequence, compress into an output folder, then point the
Thumbnails tab's input at the run folder that produced. If that folder still
holds `_minjpg_INCOMPLETE.txt`, Scan warns that it is an unfinished run.

## Building binaries

### Linux

```bash
./build.sh                  # -> dist/minjpg
./dist/minjpg --selftest    # check the build without a display
```

`--selftest` encodes a generated image with the bundled MozJPEG and confirms the
result is 4:2:0, progressive and metadata-free — a quick way to prove a fresh
build found its `cjpeg`. It then converts a JPEG and a TIFF carrying EXIF, XMP
and IPTC and checks that all of it survived, so a binary built from older code
fails its selftest instead of dropping captions and GPS in real use.

`dist/minjpg` is committed, so check it after pulling: an out-of-date build
reports the same version number but fails that metadata round-trip.

### Windows, step by step

PyInstaller **cannot cross-compile** — it embeds the interpreter it runs on — so
`minjpg.exe` has to be built on Windows. Nothing needs to be compiled, though:
every dependency installs as a ready-made package, and the Windows MozJPEG
encoder is already committed under `vendor/`. A first build takes about five
minutes.

**1. Install Python.** Get it from
[python.org/downloads/windows](https://www.python.org/downloads/windows/) and
pick **Windows installer (64-bit)** for version **3.12, 3.13 or 3.14**.

> The version matters: below 3.12 there is no ready-made `numpy` package, and
> above 3.14 some dependencies have none yet — pip would try to compile from
> source and fail without a full C++ toolchain. Prefer the python.org installer
> over the Microsoft Store build, which is known to cause trouble with tkinter.

In the installer:

- Tick **Add python.exe to PATH** on the first screen.
- Leave **tcl/tk and IDLE** ticked — that is the GUI toolkit this app uses.

**2. Get the code.** Either install [Git for Windows](https://git-scm.com/download/win)
and clone it, or download the repository as a ZIP and extract it. Avoid a path
with unusual characters, and keep it reasonably short (e.g. `C:\dev\minjpg`)
since PyInstaller and long Windows paths do not always mix.

**3. Open PowerShell in the project folder.** In File Explorer, open the folder
holding `build_windows.ps1`, then click the address bar, type `powershell`, and
press Enter.

**4. Build.**

```powershell
powershell -ExecutionPolicy Bypass -File build_windows.ps1
```

The `-ExecutionPolicy Bypass` is needed because Windows blocks unsigned scripts
by default; it applies to this one command only and changes nothing permanently.

The script checks your Python first and stops with a plain explanation if
something is wrong, then creates a `.venv`, installs the pinned dependencies,
verifies the bundled encoder's checksum, and runs PyInstaller. You end up with:

```
dist\minjpg.exe
```

**5. Check it.**

```powershell
.\dist\minjpg.exe --selftest
```

It should report the MozJPEG version, the format list, a test encode, a HEIC
round-trip and a metadata round-trip, ending in `selftest OK`. The app is built windowed, so there is no
console to print to — `--selftest` shows the report in a dialog and also writes
it to `%APPDATA%\minjpg\minjpg.log`.

Then double-click `dist\minjpg.exe` and run a real folder through both tabs.

`minjpg.exe` is self-contained: copy it anywhere, no Python needed on the target
machine.

#### If it goes wrong

| Symptom | Fix |
| --- | --- |
| `python` is not recognised | PATH was not ticked during install. Re-run the installer, choose **Modify**, tick **Add python.exe to PATH**, then open a **new** PowerShell window. |
| `running scripts is disabled on this system` | You left out `-ExecutionPolicy Bypass`. Use the exact command in step 4. |
| `Found Python 3.11, which this build does not support` | Install 3.12–3.14 as above. If several versions are installed, the script uses whichever `python` resolves to first on PATH. |
| `This Python has no tkinter` | Re-run the installer → **Modify** → tick **tcl/tk and IDLE**. |
| pip spends minutes compiling, then fails | Almost always an unsupported Python version or a 32-bit install — check `python -c "import sys, platform; print(sys.version, platform.machine())"` reports 3.12–3.14 and `AMD64`. |
| `does not match its recorded checksum` | `vendor\cjpeg-windows-x86_64.exe` was altered or corrupted in transit. Restore it with `.venv\Scripts\python.exe tools\fetch_cjpeg.py --windows`. |
| Windows SmartScreen warns on first run | Expected for any unsigned executable. Choose **More info → Run anyway**. Signing it needs a code-signing certificate. |
| Antivirus quarantines the `.exe` | A known PyInstaller false positive, not specific to this app. Add an exclusion for the `dist` folder, or build the folder version instead of one-file. |
| The build succeeds but the window never opens | Look at `%APPDATA%\minjpg\minjpg.log` — the crash is recorded there. |

#### Building on Linux instead

There is no supported way to cross-compile, but you can run *Windows* Python
under Wine, which produces a genuine `.exe`. With Docker installed:

```bash
docker run --rm -v "$PWD:/src" tobix/pywine:3.12 sh -c \
  "wine pip install -r requirements.txt && wine pyinstaller --noconfirm --clean minjpg.spec"
```

Everything this app needs cooperates — all dependencies have ready-made Windows
packages, the Windows `cjpeg.exe` is already vendored, and `minjpg.spec` picks it
automatically because `platform.system()` under that Python reports `Windows`.
Treat the result as untested until it has been run on real Windows, though: a
Wine-built binary is far less exercised than one built natively.

## The bundled encoder

`vendor/` holds MozJPEG 4.0.3 `cjpeg`:

- `cjpeg-linux-x86_64` — built from source, fully static (no runtime deps).
- `cjpeg-windows-x86_64.exe` — Mozilla's official `cjpeg-static.exe`.

Verify or recreate them with:

```bash
python tools/fetch_cjpeg.py --check       # verify checksums
python tools/fetch_cjpeg.py --linux       # rebuild Linux (needs cmake)
python tools/fetch_cjpeg.py --windows     # re-download Windows
```

The Linux build is done from source on purpose: the only published Linux
prebuild (imagemin/mozjpeg-bin) is dynamically linked against a `libjpeg.so.62`
it does not ship, so it would either fail to start or silently fall back to the
system libjpeg-turbo — which is not MozJPEG and produces different files.

MozJPEG is BSD/IJG licensed; see `vendor/LICENSE.mozjpeg.md`.

> Note: Squoosh pins MozJPEG **3.3.1** while these binaries are **4.0.3**. Both
> use the same ImageMagick quantization table and the same quality scaling, so
> output is equivalent; the byte-targeting search absorbs any small difference.

## Verifying

```bash
.venv/bin/python tools/verify_convert.py              # 277 checks
.venv/bin/python tools/verify_gui.py                  # 107 checks, needs a display
.venv/bin/python tools/verify_against_examples.py --all   # _min: all 197 pairs
.venv/bin/python tools/fingerprint.py --compare FILE  # what changed on disk
```

Each suite reads its sample photos from `../example_data`, or from `--data DIR`.
Without them, add `--synthetic` to run on generated stand-in photos instead.

Read the exit status, not just the last line: **0** means every check passed,
**1** that a check failed, and **2** that sections were skipped — no sample
photos, no display, or an ICC profile missing on this machine — so those parts
were *not* verified. A skipped run never reports success.

`verify_convert.py` covers every input format, wide-gamut colour conversion
against a real Adobe RGB profile, EXIF keep/strip behaviour, the size cap and
passthrough rules, run-folder naming and collision handling, the input/output
overlap guards, both thumbnail layouts, the full-mirror copy, all of the
destructive-write guards above, and the atomic write helpers — including temp
names that cannot clash, short copies that never land, and kept file dates. It
covers XMP and IPTC carried across from JPEG, PNG, WebP, TIFF and HEIC (captions,
keywords, ratings, with stale facts corrected and oversized packets slimmed),
16-bit sources, CMYK and greyscale profiles, multi-frame files kept whole,
content-based passthrough, the fallback names reserved for unreadable images, and
linked and unreadable folders. It covers what used to vanish without a word — a
TIFF's EXIF, EXIF in PNG text, extended XMP, colour profiles that cannot be
applied, file-manager tags and drives that cannot hold them — as well as the
naming rules (a real JPEG keeps its name, `trip_min-2024` is a photo, `._` files
are not), the refusal to create a missing output folder, and the run lifecycle:
when a broken encoder fails rather than keeps originals, when the check at the
end keeps a folder marked incomplete, and that a run folder vanishing mid-run
stops the run. It also asserts the source tree is byte-for-byte unchanged after
a run of each mode.

`verify_gui.py` drives the real app: it builds both tabs, checks each refuses to
act until both folders are named, runs every layout, and then asserts the input
tree is byte-for-byte and mtime unchanged, that repeat runs step aside with a
suffix instead of overwriting, that Start's dialog names the folder it is about
to create, that editing a folder *or any option* after Scan blocks Start, that
"Re-do selected" cannot create the run folder before Start has confirmed it, and
that a run which writes nothing removes its own empty folder. It also checks that
every row in the "beside" layout reports its own result; that a failure, a cancel
and a full disk each leave the incomplete marker (and a successful re-do removes
it); that an unreadable image is carried across as **kept original**; that one
bad event cannot freeze a tab; and that an EXIF-rotated photo previews upright.
Finally it checks that Settings-tab fields take effect at the next Scan and a run
keeps the settings its scan took, that Reset and a missing output folder both
ask first, that renamed outputs, metadata losses and colour doubts show on their
rows and in the end-of-run dialog, and that tab titles quote no stale numbers.

`verify_against_examples.py` compresses real originals from `../example_data`
and prints the result next to the hand-made Squoosh `_min.jpg`, asserting every
generated file is inside the hard cap and really carries the ImageMagick
quantization table at the expected quality, 4:2:0 chroma, progressive coding and
no metadata. With `--synthetic` the size comparison means nothing, since the
references are plain Pillow thumbnails, but those assertions still hold.

`fingerprint.py` records everything the app writes for a fixed set of awkward
inputs, built once under `.verify-out/fingerprint/corpus`: every job, every
row's outcome, the incomplete-marker verdict, and each output file's hash, date
and metadata, across both tabs and every layout. It also confirms that no run
changed the input. Save a baseline before a change with `--save FILE`, then
`--compare FILE` afterwards: a refactor should report *identical*, and a fix
should change only what it set out to. Exit status 2 there means the two runs
are not comparable (different input files or library versions).

Expect the app's files to be **larger than the old ones on average** — that is
the point of the defaults. Where a photo was hand-shrunk to 600×840 at 38 KB,
the app keeps 914×1280 and spends the available budget.
