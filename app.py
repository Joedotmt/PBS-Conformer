import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from fractions import Fraction
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    TkinterDnD = None


VIDEO_EXTENSIONS = set(".mxf .mov .mp4 .m4v .avi .mkv .mts .m2ts .ts .mpg .mpeg .m2v .vob .webm .wmv .flv .3gp .ogv .dv .m2t".split())
LOUDNESS_TOLERANCE = 0.2  # LU; measurement/rounding tolerance, not a different target.
# loudnorm measures the programme; a 192 kHz limiter enforces the lower peak ceiling.
LOUDNORM = "loudnorm=I=-23:TP=-9:LRA=50"


def media_tool(name):
    """Prefer the tools extracted from the single-file Windows bundle."""
    if getattr(sys, "frozen", False):
        bundled = Path(sys._MEIPASS) / "tools" / f"{name}.exe"
        if bundled.is_file():
            return str(bundled)
    return shutil.which(name)


class Cancelled(Exception):
    pass


class Runner:
    """One cancellable child process; stdout is streamed, stderr stays on disk."""

    def __init__(self):
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.process = None

    def check(self):
        if self.stop.is_set():
            raise Cancelled()

    def cancel(self):
        self.stop.set()
        with self.lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()

    def run(self, args, line=None, strict=False):
        self.check()
        if args[0] in ("ffmpeg", "ffprobe"):
            executable = media_tool(args[0])
            if not executable:
                raise FileNotFoundError(f"{args[0]} is missing. Install FFmpeg or rebuild the executable with its bundled tools.")
            args = [executable, *args[1:]]
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errors:
            with self.lock:
                self.check()
                self.process = subprocess.Popen(
                    args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=errors, text=True, encoding="utf-8", errors="replace",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                process = self.process
            output = []
            try:
                for value in process.stdout:
                    self.check()
                    if line:
                        line(value.rstrip())
                    else:
                        output.append(value)
                code = process.wait()
                self.check()
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                process.stdout.close()
                with self.lock:
                    self.process = None
            errors.seek(0, os.SEEK_END)
            length = errors.tell()
            errors.seek(max(0, length - 16000))
            log = errors.read()
            if code or (strict and log.strip()):
                raise RuntimeError(log.strip()[-2500:] or f"{args[0]} exited with code {code}.")
            return "".join(output), log

    def ffmpeg(self, args, duration, progress):
        def update(line):
            if line.startswith("out_time_us="):
                try:
                    progress(min(1.0, max(0.0, int(line.split("=", 1)[1]) / 1e6 / duration)))
                except (ValueError, ZeroDivisionError):
                    pass
        return self.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info",
             "-xerror", "-progress", "pipe:1", *args], line=update,
        )[1]


def probe(path, runner):
    data, _ = runner.run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                          "-of", "json", str(path)], strict=True)
    return json.loads(data)


def streams(info, kind):
    return [s for s in info.get("streams", []) if s.get("codec_type") == kind
            and not s.get("disposition", {}).get("attached_pic")]


def duration_of(info):
    candidates = [s.get("duration") for s in streams(info, "video")]
    candidates.append(info.get("format", {}).get("duration"))
    for value in candidates:
        try:
            duration = float(value)
            if math.isfinite(duration) and duration > 0:
                return duration
        except (TypeError, ValueError):
            pass
    raise ValueError("Cannot determine the video duration.")


def programme_tracks(info):
    """Prefer the first stereo programme, then the first pair of mono tracks."""
    audio = streams(info, "audio")
    if not audio:
        raise ValueError("Source has no audio. A programme mix is needed to meet -23 LUFS.")
    stereo = [i for i, stream in enumerate(audio) if stream.get("channels") == 2]
    mono = [i for i, stream in enumerate(audio) if stream.get("channels") == 1]
    return stereo[:1] if stereo else mono[:2] if len(mono) >= 2 else [0]


def audio_graph(info, duration=None):
    """Assemble the programme as stereo, preserving A=Left and B=Right."""
    audio = streams(info, "audio")
    selected = programme_tracks(info)
    first = audio[selected[0]]
    label = f"[0:a:{selected[0]}]"
    if len(selected) == 2:
        graph = (f"{label}aresample=48000[a0];[0:a:{selected[1]}]aresample=48000[a1];"
                 "[a0][a1]join=inputs=2:channel_layout=stereo:map=0.0-FL|1.0-FR")
    elif first.get("channels") == 1:
        graph = label + "pan=stereo|c0=c0|c1=c0"
    else:
        channels = first.get("channels", 0)
        if channels < 1:
            raise ValueError("Source audio has no decodable channels.")
        layout = first.get("channel_layout", "")
        if channels > 2 and (not layout or layout == "unknown" or re.fullmatch(r"\d+ channels", layout)):
            # Discrete, unlabelled channels have no surround speaker assignments.
            # Use all channels: odd-numbered channels left, even-numbered right.
            left = "+".join(f"c{i}" for i in range(0, channels, 2))
            right = "+".join(f"c{i}" for i in range(1, channels, 2))
            graph = f"{label}aformat=sample_fmts=fltp,pan=stereo|c0<{left}|c1<{right}"
        else:
            # FFmpeg's speaker-aware matrix includes centre/surround contributions.
            graph = label + "aformat=sample_fmts=fltp:channel_layouts=stereo"
    if duration is not None:
        # Keep the programme video length, padding short audio and trimming long audio.
        graph += f",aresample=48000:async=1:first_pts=0,apad,atrim=duration={duration:.9f}"
    return graph + "[programme]"


def measure(path, info, runner, progress, conform=False):
    duration = duration_of(info)
    graph = audio_graph(info, round(duration * 25) / 25 if conform else None)
    log = runner.ffmpeg(
        ["-i", str(path), "-filter_complex", graph + f";[programme]{LOUDNORM}:print_format=json[meter]",
         "-map", "[meter]", "-vn", "-f", "null", "-"], duration, progress,
    )
    matches = re.findall(r'\{\s*"input_i".*?\}', log, re.S)
    if not matches:
        raise ValueError("FFmpeg did not return a loudness measurement.")
    return json.loads(matches[-1])


def prepare_audio(source, info, folder, runner, progress):
    """Measured gain plus true-peak limiting, remeasured until both targets hold.

    Keep stereo linked during limiting. Only audio is retried; video is encoded once.
    Very peaky material can need several passes as limiting changes gated loudness.
    """
    current = source
    duration = round(duration_of(info) * 25) / 25
    for attempt in range(9):
        stage = "Measuring audio" if attempt == 0 else f"Checking normalized audio (pass {attempt})"
        stats = measure(current, info, runner, lambda f: progress(0.02 + attempt * 0.02 + f * 0.008, stage), conform=attempt == 0)
        loudness, peak = float(stats["input_i"]), float(stats["input_tp"])
        if not math.isfinite(loudness) or not math.isfinite(peak):
            raise ValueError("Silent or unmeasurable audio cannot meet -23 LUFS.")
        if attempt and abs(loudness + 23) <= LOUDNESS_TOLERANCE and peak <= -12:
            return current
        if attempt == 8:
            raise ValueError(f"Audio could not reach both targets: {loudness:.2f} LUFS / {peak:.2f} dBTP. Supply a mastered mix.")
        target = folder / f"audio-{attempt}.wav"
        stage = f"Normalizing audio (pass {attempt + 1})"
        # 0.5 dB margin accommodates resampling and the measurement's 0.01 dB resolution.
        graph = (audio_graph(info, duration) + f";[programme]volume={-23 - loudness:.6f}dB,"
                 f"aresample=192000,alimiter=limit={10 ** (-12.5 / 20):.9f}:level=false:latency=true,"
                 "aresample=48000[normalized]")
        runner.ffmpeg(["-n", "-i", str(current), "-filter_complex", graph, "-map", "[normalized]",
                       "-c:a", "pcm_f32le", "-rf64", "auto", str(target)], duration,
                      lambda f: progress(0.03 + attempt * 0.02 + f * 0.008, stage))
        if current != source:
            current.unlink()
        current = target
        info = probe(current, runner)


def technical_failures(info):
    failures = []
    fmt = info.get("format", {})
    if fmt.get("format_name") != "mxf":
        failures.append("Container must be MXF OP1a.")
    ul = re.sub(r"[^0-9a-f]", "", fmt.get("tags", {}).get("operational_pattern_ul", "").lower())
    if len(ul) != 32 or ul[:14] != "060e2b34040101" or ul[16:28] != "0d0102010101":
        failures.append("MXF operational pattern is not OP1a (or is missing).")
    video = streams(info, "video")
    if len(video) != 1:
        failures.append(f"Expected one video stream; found {len(video)}.")
    if video:
        v = video[0]
        expected = {
            "codec_name": ("mpeg2video", "MPEG-2 video"),
            "profile": ("4:2:2", "MPEG-2 4:2:2 profile"),
            "level": (2, "MPEG-2 High Level"),
            "pix_fmt": ("yuv422p", "8-bit 4:2:2 video"),
            "width": (1920, "1920-pixel width"), "height": (1080, "1080-line height"),
            "color_space": ("bt709", "Rec.709 matrix"),
            "color_transfer": ("bt709", "Rec.709 transfer"),
            "color_primaries": ("bt709", "Rec.709 primaries"),
            "color_range": ("tv", "limited video range"),
            "sample_aspect_ratio": ("1:1", "square pixels"),
        }
        for key, (wanted, label) in expected.items():
            if v.get(key) != wanted:
                failures.append(f"Expected {label}; found {v.get(key, 'unknown')}.")
        for key in ("r_frame_rate", "avg_frame_rate"):
            try:
                valid = Fraction(v.get(key, "0")) == 25
            except (ValueError, ZeroDivisionError):
                valid = False
            if not valid:
                failures.append(f"Expected 25 fps ({key}); found {v.get(key, 'unknown')}.")
        if v.get("field_order") not in ("tt", "tb"):
            failures.append(f"Expected Upper Field First interlace; found {v.get('field_order', 'unknown')}.")
        if str(v.get("bit_rate")) != "50000000":
            failures.append(f"Expected 50 Mbps video; found {v.get('bit_rate', 'unknown')} bps.")
        # MXF identifies MPEG essence with SMPTE labels, not MOV/AVI FourCCs.
        if v.get("codec_tag_string", "") not in ("", "[0][0][0][0]", "xd5c"):
            failures.append(f"Unexpected video codec tag: {v['codec_tag_string']}.")
    audio = streams(info, "audio")
    if len(audio) != 2:
        failures.append(f"Expected two separate mono audio streams (A=L, B=R); found {len(audio)}.")
    for i, a in enumerate(audio, 1):
        if a.get("channels") != 1:
            failures.append(f"Audio {i}: expected mono; found {a.get('channels', 'unknown')} channels.")
        if a.get("codec_name") != "pcm_s24le" or a.get("bits_per_sample") != 24:
            failures.append(f"Audio {i}: expected 24-bit PCM.")
        if str(a.get("sample_rate")) != "48000":
            failures.append(f"Audio {i}: expected 48 kHz.")
        if video:
            try:
                if abs(float(a["duration"]) - duration_of(info)) > 0.04:
                    failures.append(f"Audio {i}: duration differs from video by more than one frame.")
                if abs(float(a.get("start_time", 0)) - float(video[0].get("start_time", 0))) > 0.04:
                    failures.append(f"Audio {i}: start differs from video by more than one frame.")
            except (KeyError, TypeError, ValueError):
                failures.append(f"Audio {i}: timing cannot be verified.")
    return failures


def verify(path, runner, progress=lambda fraction, stage: None):
    info = probe(path, runner)
    failures = technical_failures(info)
    duration = duration_of(info)
    frame_errors = set()
    count = 0
    previous = None

    def frame(line):
        nonlocal count, previous
        values = dict(part.split("=", 1) for part in line.split("|") if "=" in part)
        if "width" not in values:
            return
        count += 1
        if values.get("interlaced_frame") != "1" or values.get("top_field_first") != "1":
            frame_errors.add("Decoded video contains frames that are not interlaced Upper Field First.")
        if any(values.get(k) != v for k, v in {
            "width": "1920", "height": "1080", "pix_fmt": "yuv422p", "color_space": "bt709",
            "color_primaries": "bt709", "color_transfer": "bt709", "color_range": "tv",
        }.items()):
            frame_errors.add("Decoded video contains frames outside the HD422 / Rec.709 specification.")
        try:
            pts = float(values["best_effort_timestamp_time"])
            if previous is not None and abs(pts - previous - 0.04) > 0.001:
                frame_errors.add("Decoded video cadence is not constant 25 fps.")
            previous = pts
        except (KeyError, ValueError):
            frame_errors.add("Video frame timestamps are missing.")
        if count % 25 == 0:
            progress(min(0.6, count / (25 * duration) * 0.6), "Checking video frames")

    progress(0, "Checking video frames")
    if streams(info, "video"):
        try:
            runner.run(["ffprobe", "-v", "error", "-err_detect", "explode", "-select_streams", str(streams(info, "video")[0]["index"]),
                        "-show_frames", "-show_entries",
                        "frame=width,height,pix_fmt,interlaced_frame,top_field_first,best_effort_timestamp_time,color_space,color_primaries,color_transfer,color_range:frame_side_data=",
                        "-of", "compact=p=0", str(path)], line=frame, strict=True)
        except RuntimeError as exc:
            failures.append(f"Video decode failed: {exc}")
    if not count:
        frame_errors.add("No video frames could be decoded.")
    elif abs(count / 25 - duration) > 0.08:
        frame_errors.add("Decoded video length does not match the declared 25 fps duration.")
    failures.extend(sorted(frame_errors))
    progress(0.6, "Measuring programme loudness / true peak")
    levels = ""
    try:
        stats = measure(path, info, runner, lambda f: progress(0.6 + f * 0.4, "Measuring programme loudness / true peak"))
        loudness, peak = float(stats["input_i"]), float(stats["input_tp"])
        levels = f"{loudness:.2f} LUFS · {peak:.2f} dBTP"
        if not math.isfinite(loudness) or abs(loudness + 23) > LOUDNESS_TOLERANCE + 1e-6:
            failures.append(f"Integrated loudness {loudness:.2f} LUFS; required -23 ±{LOUDNESS_TOLERANCE} LUFS.")
        if not math.isfinite(peak) or peak > -12:
            failures.append(f"True peak {peak:.2f} dBTP; maximum -12 dBTP.")
    except (ValueError, RuntimeError) as exc:
        failures.append(f"Loudness could not be verified: {exc}")
    progress(1, "Verified")
    return failures, levels


def video_filter(video):
    # Use source tags when known; conventional SDR defaults only for missing tags.
    sd = video.get("height", 1080) <= 576
    default = "bt470bg" if sd and video.get("height", 0) > 480 else "smpte170m" if sd else "bt709"
    color = "colorspace=all=bt709:range=tv:format=yuv422p"
    for key, option, fallback in (("color_space", "ispace", default),
                                   ("color_primaries", "iprimaries", default),
                                   ("color_transfer", "itrc", "bt709"),
                                   ("color_range", "irange", "tv")):
        if video.get(key) in (None, "unknown", "unspecified", "reserved"):
            color += f":{option}={fallback}"
    if video.get("color_transfer") in ("smpte2084", "arib-std-b67"):
        # Convert PQ/HLG to linear light, tone-map, then encode SDR Rec.709.
        linear = "zscale=t=linear:npl=100"
        for key, option, fallback in (("color_space", "min", "bt2020nc"),
                                      ("color_primaries", "pin", "bt2020"),
                                      ("color_range", "rin", "limited")):
            if video.get(key) in (None, "unknown", "unspecified", "reserved"):
                linear += f":{option}={fallback}"
        color = (linear + ",format=gbrpf32le,tonemap=tonemap=mobius:desat=2,"
                 "zscale=p=bt709:t=bt709:m=bt709:r=limited,format=yuv422p")
    return ("bwdif=mode=send_field:parity=auto:deint=interlaced,fps=50,"
            "scale=w='trunc(min(1920,1080*dar)/2)*2':h='trunc(min(1080,1920/dar)/2)*2',"
            f"setsar=1,{color},pad=1920:1080:(ow-iw)/2:(oh-ih)/2,"
            "tinterlace=mode=interleave_top,setfield=tff,"
            "setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709")


def convert(source, target, info, runner, progress=lambda fraction, stage: None):
    video = streams(info, "video")
    if not video:
        raise ValueError("No decodable video stream found.")
    vf = video_filter(video[0])
    # Round the programme length to an output frame; avoid truncation to audio length.
    duration = round(duration_of(info) * 25) / 25
    if duration < 0.4:
        raise ValueError("Video is too short for an integrated loudness measurement (minimum 0.4 seconds).")
    progress(0, "Analysing audio")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Reserve our destination exclusively. FFmpeg only writes inside our private temp directory.
    with target.open("xb"):
        pass
    try:
        with tempfile.TemporaryDirectory(prefix=".pbs-", dir=target.parent) as temp:
            pending = Path(temp) / "output.mxf"
            audio = prepare_audio(source, info, Path(temp), runner, progress)
            progress(0.20, "Converting")
            runner.ffmpeg([
                "-n", "-i", str(source), "-i", str(audio), "-filter_complex",
                "[1:a:0]channelsplit=channel_layout=stereo[left][right]",
                "-map", f"0:{video[0]['index']}", "-map", "[left]", "-map", "[right]", "-map_metadata", "-1",
                "-vf", vf, "-t", str(duration), "-c:v", "mpeg2video", "-pix_fmt", "yuv422p",
                "-b:v", "50M", "-minrate", "50M", "-maxrate", "50M", "-bufsize", "17825792",
                "-g", "12", "-bf", "2", "-sc_threshold", "1000000000", "-flags", "+ildct+ilme",
                "-alternate_scan", "1", "-profile:v", "0", "-level:v", "2",
                "-tag:v", "xd5c", "-seq_disp_ext", "1", "-color_primaries", "bt709",
                "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
                "-c:a", "pcm_s24le", "-ar", "48000", "-ac", "1",
                "-metadata:s:a:0", "title=A - Left", "-metadata:s:a:1", "title=B - Right",
                "-threads", "2", "-f", "mxf", str(pending),
            ], duration, lambda f: progress(0.20 + f * 0.50, "Converting"))
            failures, levels = verify(pending, runner, lambda f, s: progress(0.70 + f * 0.30, s))
            if failures:
                raise ValueError("Automatic verification failed:\n" + "\n".join(failures))
            runner.check()
            pending.replace(target)  # Replaces only the empty file we exclusively reserved above.
        audio = streams(info, "audio")
        selected = programme_tracks(info)
        if len(audio) > len(selected):
            used = "/".join(str(i + 1) for i in selected)
            levels += f" · Source audio {used} used; {len(audio) - len(selected)} extra tracks omitted"
        if len(video) > 1:
            levels += " · First video stream used"
        return levels
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def discover(root, runner, status):
    def walk_error(error):
        raise error

    files = [root] if root.is_file() else (
        Path(folder) / name for folder, _, names in os.walk(root, onerror=walk_error)
        for name in sorted(names)
    )
    found = []
    for path in files:
        runner.check()
        if path.is_symlink() or not path.is_file():
            continue
        status(f"Scanning: {path.name}")
        try:
            info = probe(path, runner)
            if streams(info, "video"):
                found.append((path, info, None))
            elif root.is_file() or path.suffix.lower() in VIDEO_EXTENSIONS:
                found.append((path, None, "No video stream found."))
        except (RuntimeError, ValueError) as exc:
            if root.is_file() or path.suffix.lower() in VIDEO_EXTENSIONS:
                found.append((path, None, str(exc)))
    return sorted(found, key=lambda item: str(item[0]).casefold())


def new_output_folder(source):
    for number in range(1, 10000):
        target = source.with_name(source.name + "_broadcast" + (f"_{number}" if number > 1 else ""))
        try:
            target.mkdir()
            return target
        except FileExistsError:
            continue
    raise ValueError("Unable to create a new output folder.")


def output_paths(files, source, output):
    used = {str(parent).casefold() for path, _, _ in files
            for parent in path.relative_to(source).parents if parent != Path(".")}
    for path, _, _ in files:
        relative = path.relative_to(source).with_suffix(".mxf")
        target = relative
        number = 1
        while str(target).casefold() in used:
            extra = "" if number == 1 else f"_{number}"
            target = relative.with_name(path.name + extra + ".mxf")
            number += 1
        used.add(str(target).casefold())
        yield output / target


def native_picker_command(folder, initial):
    """Use the installed desktop helper without adding GTK/Qt Python bindings."""
    if not sys.platform.startswith("linux"):
        return None
    desktop = (os.environ.get("XDG_CURRENT_DESKTOP", "") + ":" +
               os.environ.get("DESKTOP_SESSION", "")).lower()
    helpers = ("kdialog", "zenity") if "kde" in desktop or "plasma" in desktop else ("zenity", "kdialog")
    title = "Select a folder" if folder else "Select a video"
    for helper in helpers:
        executable = shutil.which(helper)
        if not executable:
            continue
        if helper == "kdialog":
            return [executable, "--title", title,
                    "--getexistingdirectory" if folder else "--getopenfilename", str(initial)]
        return [executable, "--file-selection", f"--title={title}",
                f"--filename={str(initial).rstrip(os.sep) + os.sep}", *(["--directory"] if folder else [])]
    return None


class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.worker = None
        self.runner = None
        self.picker = None
        self.closing = False
        root.title("PBS Conformer")
        root.geometry("800x640")
        root.minsize(660, 560)
        style = ttk.Style(root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TButton", padding=(12, 7))
        style.configure("Action.TButton", font=("TkDefaultFont", 11, "bold"))
        style.configure("TNotebook.Tab", padding=(24, 9))
        style.map("TNotebook.Tab", padding=[("selected", (24, 9))])
        outer = ttk.Frame(root, padding=18)
        outer.pack(fill="both", expand=True)
        notebook = ttk.Notebook(outer)
        self.tabs = {}
        self.controls = []
        for mode in ("Convert", "Verify"):
            tab = ttk.Frame(notebook, padding=14)
            notebook.add(tab, text=mode)
            hint = "Drop a source folder here" if mode == "Convert" else "Drop a video or folder here"
            if not TkinterDnD:
                hint = "Select a source folder" if mode == "Convert" else "Select a video or folder"
            drop = ttk.Label(tab, text=hint, anchor="center", padding=16, relief="groove")
            drop.pack(fill="x", pady=(0, 12))
            path = tk.StringVar()
            row = ttk.Frame(tab)
            row.pack(fill="x")
            entry = ttk.Entry(row, textvariable=path)
            entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
            self.controls.append(entry)
            for label, is_folder in (("Folder…", True),) if mode == "Convert" else (("File…", False), ("Folder…", True)):
                button = ttk.Button(row, text=label, command=lambda p=path, d=is_folder: self.browse(p, d))
                button.pack(side="left", padx=(0, 4))
                self.controls.append(button)
            if TkinterDnD:
                for widget in (drop, entry):
                    widget.drop_target_register(DND_FILES)
                    widget.dnd_bind("<<Drop>>", lambda e, p=path, m=mode: self.drop(e, p, m))
            button = ttk.Button(tab, text=mode, style="Action.TButton", command=lambda m=mode: self.start(m))
            button.pack(anchor="e")
            self.controls.append(button)
            bar = ttk.Progressbar(tab, maximum=100)
            bar.pack(fill="x", pady=(12, 6))
            status = tk.StringVar(value="Ready")
            ttk.Label(tab, textvariable=status, wraplength=560).pack(anchor="w")
            results = ScrolledText(tab, height=9, wrap="word", font=("TkDefaultFont", 10),
                                   relief="flat", borderwidth=0, padx=10, pady=10, state="disabled")
            results.pack(fill="both", expand=True, pady=(12, 0))
            results.tag_configure("pass", foreground="#167347")
            results.tag_configure("fail", foreground="#b42318")
            self.tabs[mode] = dict(path=path, bar=bar, status=status, results=results)
        ttk.Label(outer, text="MXF OP1a · HD422 / 1080i50 · A=L / B=R · −23 LUFS · ≤ −12 dBTP",
                  wraplength=580).pack(side="bottom", anchor="w", pady=(14, 0))
        notebook.pack(fill="both", expand=True)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)
        if not TkinterDnD:
            self.tabs["Convert"]["status"].set("Drag and drop unavailable. Install requirements.txt; browsing still works.")

    def browse(self, variable, folder):
        if self.closing or self.picker or (self.worker and self.worker.is_alive()):
            return
        initial = Path(variable.get()).expanduser() if variable.get() else Path.home()
        if not initial.is_dir():
            initial = initial.parent if initial.parent.is_dir() else Path.home()
        initial = initial.resolve()
        command = native_picker_command(folder, initial)
        if command:
            try:
                self.picker = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except OSError:
                pass  # The helper disappeared or could not start; use Tk below.
            else:
                for control in self.controls:
                    control.configure(state="disabled")
                self.root.after(100, lambda: self.finish_browse(variable, folder, initial))
                return
        self.tk_browse(variable, folder, initial)

    def tk_browse(self, variable, folder, initial):
        choose = filedialog.askdirectory if folder else filedialog.askopenfilename
        value = choose(parent=self.root, initialdir=str(initial))
        if value:
            variable.set(value)

    def finish_browse(self, variable, folder, initial):
        if self.picker.poll() is None:
            self.root.after(100, lambda: self.finish_browse(variable, folder, initial))
            return
        output, _ = self.picker.communicate()
        code = self.picker.returncode
        self.picker = None
        if self.closing:
            return
        for control in self.controls:
            control.configure(state="normal")
        if code == 0:
            # Remove only the helper's final newline; spaces can be part of a filename.
            value = os.fsdecode(output).removesuffix("\n")
            if value:
                variable.set(value)
        elif code != 1:  # Cancel must not open a second dialog.
            self.tk_browse(variable, folder, initial)

    def drop(self, event, variable, mode):
        if self.picker or (self.worker and self.worker.is_alive()):
            return "refuse_drop"
        paths = self.root.tk.splitlist(event.data)
        if len(paths) != 1 or (mode == "Convert" and not Path(paths[0]).is_dir()):
            messagebox.showerror("Choose one source", "Drop one folder." if mode == "Convert" else "Drop one video or folder.")
            return "refuse_drop"
        variable.set(paths[0])
        return "copy"

    def start(self, mode):
        if self.picker or (self.worker and self.worker.is_alive()):
            return
        missing = [name for name in ("ffmpeg", "ffprobe") if not media_tool(name)]
        if missing:
            messagebox.showerror("FFmpeg required", "Install FFmpeg and add ffmpeg and ffprobe to PATH.\nMissing: " + ", ".join(missing))
            return
        value = self.tabs[mode]["path"].get().strip()
        source = Path(value).expanduser().resolve()
        if not value or not source.exists() or (mode == "Convert" and not source.is_dir()):
            messagebox.showerror("Select a source", "Choose a folder." if mode == "Convert" else "Choose a video or folder.")
            return
        if mode == "Convert" and source == source.parent:
            messagebox.showerror("Select a source", "Choose a folder below the filesystem root.")
            return
        tab = self.tabs[mode]
        tab["results"].configure(state="normal")
        tab["results"].delete("1.0", "end")
        tab["results"].configure(state="disabled")
        tab["bar"]["value"] = 0
        tab["bar"].configure(mode="indeterminate")
        tab["bar"].start(15)
        for control in self.controls:
            control.configure(state="disabled")
        self.runner = Runner()
        self.worker = threading.Thread(target=self.work, args=(mode, source), daemon=True)
        self.worker.start()

    def work(self, mode, source):
        def send(kind, *data):
            self.events.put((mode, kind, data))
        passed = failed = 0
        try:
            files = discover(source, self.runner, lambda s: send("status", s))
            if not files:
                send("status", "No videos found.")
                return
            output = new_output_folder(source) if mode == "Convert" else None
            targets = list(output_paths(files, source, output)) if output else [None] * len(files)
            if output:
                send("log", f"Output: {output}\n\n", "")
            for i, ((path, info, error), target) in enumerate(zip(files, targets)):
                self.runner.check()
                name = str(path.relative_to(source)) if source.is_dir() else path.name
                def progress(fraction, stage):
                    send("progress", 100 * (i + fraction) / len(files), f"{i + 1}/{len(files)} · {path.name} · {stage}")
                try:
                    if error:
                        raise ValueError(error)
                    if mode == "Convert":
                        levels = convert(path, target, info, self.runner, progress)
                        reasons = []
                    else:
                        reasons, levels = verify(path, self.runner, progress)
                    if reasons:
                        raise ValueError("\n".join(reasons))
                    passed += 1
                    location = f" → {target.relative_to(output)}" if target else ""
                    send("log", f"PASS  {name}{location}\n    {levels}\n\n", "pass")
                except (OSError, ValueError, RuntimeError) as exc:
                    failed += 1
                    send("log", f"FAIL  {name}\n    " + str(exc).replace("\n", "\n    ") + "\n\n", "fail")
                progress(1, "Finished")
            send("status", f"Complete · {passed} PASS · {failed} FAIL" + (f" · Saved to {output}" if output else ""))
        except Cancelled:
            send("status", "Stopped.")
        except Exception as exc:
            send("log", f"FAIL  {exc}\n", "fail")
            send("status", "Unable to finish. See details above.")
        finally:
            send("done")

    def poll(self):
        # Bound each UI tick so a large folder never starves Tk's event loop.
        for _ in range(100):
            try:
                mode, kind, data = self.events.get_nowait()
            except queue.Empty:
                break
            tab = self.tabs[mode]
            if kind == "status":
                tab["status"].set(data[0])
            elif kind == "progress":
                tab["bar"].stop()
                tab["bar"].configure(mode="determinate", value=data[0])
                tab["status"].set(data[1])
            elif kind == "log":
                tab["results"].configure(state="normal")
                tab["results"].insert("end", data[0], data[1])
                tab["results"].see("end")
                tab["results"].configure(state="disabled")
            elif kind == "done":
                value = tab["bar"]["value"] if str(tab["bar"]["mode"]) == "determinate" else 0
                tab["bar"].stop()
                tab["bar"].configure(mode="determinate", value=value)
                tab["results"].yview_moveto(0)
                for control in self.controls:
                    control.configure(state="normal")
        if self.closing and self.picker is None and not (self.worker and self.worker.is_alive()):
            self.root.destroy()
        else:
            self.root.after(100, self.poll)

    def close(self):
        self.closing = True
        if self.runner:
            self.runner.cancel()
        if self.picker and self.picker.poll() is None:
            process = self.picker
            process.terminate()
            self.root.after(1000, lambda: process.kill() if process.poll() is None else None)


def main():
    root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
