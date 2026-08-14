from __future__ import annotations

import ctypes
import math
import random
import re
import time
import tkinter as tk
from pathlib import Path

APP_NAME = "冬马和纱桌宠"
TRANSPARENT = "#010203"
ASSET_DIR = Path(__file__).with_name("assets")
SPRITE_PATHS = (
    ASSET_DIR / "kazusa-q-display.png",
    ASSET_DIR / "kazusa-q-pose2-display.png",
)
STUDY_CHECK_IN_MS = 25 * 60 * 1000
LONG_SIT_MS = 45 * 60 * 1000
SINGLE_INSTANCE_MUTEX = "Local\\KazusaDesktopPet.SingleInstance"
_mutex_handle: int | None = None


def acquire_single_instance() -> bool:
    """Return False when another desktop-pet process already owns the mutex."""
    global _mutex_handle
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        create_mutex.restype = ctypes.c_void_p
        handle = create_mutex(None, False, SINGLE_INSTANCE_MUTEX)
        if not handle:
            return True
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return False
        _mutex_handle = handle
        return True
    except (AttributeError, OSError):
        # The app is Windows-targeted; failing open is safer than preventing launch.
        return True


def release_single_instance() -> None:
    global _mutex_handle
    if not _mutex_handle:
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle(ctypes.c_void_p(_mutex_handle))
    except (AttributeError, OSError):
        pass
    _mutex_handle = None


def enable_crisp_windows_dpi() -> None:
    """Prevent Windows from bitmap-stretching the whole Tk window."""
    try:
        # PER_MONITOR_AWARE_V2 on current Windows versions.
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


class KazusaPet:
    def __init__(self) -> None:
        enable_crisp_windows_dpi()
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=TRANSPARENT)
        try:
            self.root.attributes("-transparentcolor", TRANSPARENT)
        except tk.TclError:
            pass

        self.width, self.height = 300, 340
        self.canvas = tk.Canvas(
            self.root,
            width=self.width,
            height=self.height,
            bg=TRANSPARENT,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack()

        missing_sprites = [path for path in SPRITE_PATHS if not path.exists()]
        if missing_sprites:
            raise FileNotFoundError(f"缺少桌宠素材：{missing_sprites[0]}")
        # This is a pre-rendered Lanczos display cache. Loading it at 1:1 avoids
        # PhotoImage.subsample(), whose nearest-neighbour scaling causes jaggies.
        self.sprites = [tk.PhotoImage(file=str(path)) for path in SPRITE_PATHS]
        self.sprite_index = 0
        self.sprite = self.sprites[self.sprite_index]
        self.sprite_base_y = self.height - 8
        self.sprite_id = self.canvas.create_image(
            self.width // 2,
            self.sprite_base_y,
            image=self.sprite,
            anchor="s",
        )

        self.drag_origin: tuple[int, int, int, int] | None = None
        self.dragged = False
        self.muted = False
        self.bubble_job: str | None = None
        self.idle_job: str | None = None
        self.study_job: str | None = None
        self.long_sit_job: str | None = None
        self.study_mode = False
        self.timer_job: str | None = None
        self.timer_mode: str | None = None
        self.timer_end = 0.0
        self.timer_remaining = 0
        self.timer_paused = False
        self.animation_tick = 0
        self.last_interaction = time.monotonic()

        self._build_menu()
        self._bind_events()
        self._place_bottom_right()
        self._animate()
        self._schedule_idle(first=True)
        self._schedule_long_sit_reminder()
        self.root.after(700, lambda: self.say("……有事就说。", 2600))

    def _build_menu(self) -> None:
        self.menu = tk.Menu(self.root, tearoff=False, font=("Microsoft YaHei UI", 10))
        self.menu.add_command(label="让和纱说句话", command=self.react)
        self.menu.add_command(label="切换姿势", command=self.switch_pose)
        self.menu.add_command(label="开始学习", command=self.toggle_study)
        self.study_menu_index = self.menu.index("end")
        self.timer_menu = tk.Menu(
            self.menu,
            tearoff=False,
            font=("Microsoft YaHei UI", 10),
        )
        self.timer_menu.add_command(
            label="开始专注 · 25 分钟",
            command=lambda: self.start_timer("focus", 25),
        )
        self.timer_menu.add_command(
            label="开始休息 · 5 分钟",
            command=lambda: self.start_timer("break", 5),
        )
        self.timer_menu.add_separator()
        self.timer_menu.add_command(label="暂停", command=self.toggle_timer_pause, state="disabled")
        self.timer_menu.add_command(label="结束计时", command=self.stop_timer, state="disabled")
        self.menu.add_cascade(label="番茄钟", menu=self.timer_menu)
        self.menu.add_separator()
        self.menu.add_command(label="暂时安静", command=self.toggle_mute)
        self.mute_menu_index = self.menu.index("end")
        self.menu.add_command(label="退出", command=self.root.destroy)

    def _bind_events(self) -> None:
        self.canvas.bind("<ButtonPress-1>", self.start_drag)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.end_drag)
        self.canvas.bind("<Button-3>", self.show_menu)
        self.root.bind("<Escape>", lambda _e: self.root.destroy())

    def _place_bottom_right(self) -> None:
        self.root.update_idletasks()
        x = self.root.winfo_screenwidth() - self.width - 28
        y = self.root.winfo_screenheight() - self.height - 52
        self.root.geometry(f"{self.width}x{self.height}+{max(0, x)}+{max(0, y)}")

    def start_drag(self, event: tk.Event) -> None:
        self.last_interaction = time.monotonic()
        self.dragged = False
        self.drag_origin = (
            event.x_root,
            event.y_root,
            self.root.winfo_x(),
            self.root.winfo_y(),
        )

    def drag(self, event: tk.Event) -> None:
        if not self.drag_origin:
            return
        sx, sy, wx, wy = self.drag_origin
        dx, dy = event.x_root - sx, event.y_root - sy
        if abs(dx) + abs(dy) > 4:
            self.dragged = True
        self.root.geometry(f"+{wx + dx}+{wy + dy}")

    def end_drag(self, _event: tk.Event) -> None:
        if not self.dragged:
            self.react()
        self.drag_origin = None

    def show_menu(self, event: tk.Event) -> None:
        self.last_interaction = time.monotonic()
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def toggle_mute(self) -> None:
        self.muted = not self.muted
        self.menu.entryconfigure(
            self.mute_menu_index,
            label="恢复偶尔说话" if self.muted else "暂时安静",
        )
        self.say("……安静点也好。" if self.muted else "我本来就没打算多说。")

    def switch_pose(self) -> None:
        self.last_interaction = time.monotonic()
        self.sprite_index = (self.sprite_index + 1) % len(self.sprites)
        self.sprite = self.sprites[self.sprite_index]
        self.canvas.itemconfigure(self.sprite_id, image=self.sprite)
        self.canvas.coords(self.sprite_id, self.width // 2, self.sprite_base_y)
        self.say(random.choice([
            "……换个姿势而已。别大惊小怪。",
            "一直站着也会累的。有什么意见？",
        ]), 3600)

    def react(self) -> None:
        self.last_interaction = time.monotonic()
        if self.study_mode:
            self.say(random.choice([
                "……又在做什么无关紧要的事？真是没有长进。",
                "我数到三，你最好已经在做正事了。一、二——",
                "窗口切来切去的。你以为我看不见？",
                "还有时间来戳我？看来给你的任务还是太少了。",
            ]), 4600)
            return
        lines = [
            "……别一直戳。",
            "有事就直说。",
            "我没在等你。只是懒得走。",
            "别误会。我只是刚好在这里。",
            "你还真是爱管闲事。",
            "……累了就休息。不是在关心你。",
            "别盯着我看。有空就把该做的事做完。",
            "今天的进度呢？别告诉我还是零。",
            "少找借口。开始以后就没那么难了。",
            "做不下去就先整理思路。烦躁解决不了问题。",
            "……需要我陪着也不是不行。别误会。",
        ]
        self.say(random.choice(lines))

    def toggle_study(self) -> None:
        self.last_interaction = time.monotonic()
        if self.timer_mode:
            self._reset_timer()
        if self.study_mode:
            self.study_mode = False
            if self.study_job:
                self.root.after_cancel(self.study_job)
                self.study_job = None
            self.menu.entryconfigure(self.study_menu_index, label="开始学习")
            self.say("今天就到这里？……至少把收尾做好。", 4200)
            return

        self.study_mode = True
        self.menu.entryconfigure(self.study_menu_index, label="结束学习")
        self.say("哦？终于打算动了？我还以为你要在那里发霉呢。", 5000)
        self.study_job = self.root.after(STUDY_CHECK_IN_MS, self._study_check_in)

    def start_timer(self, mode: str, minutes: int) -> None:
        self.last_interaction = time.monotonic()
        self._reset_timer()
        if self.study_job:
            self.root.after_cancel(self.study_job)
            self.study_job = None

        self.timer_mode = mode
        self.timer_remaining = minutes * 60
        self.timer_end = time.monotonic() + self.timer_remaining
        self.timer_paused = False
        self.timer_menu.entryconfigure(3, label="暂停", state="normal")
        self.timer_menu.entryconfigure(4, state="normal")

        if mode == "focus":
            self.study_mode = True
            self.menu.entryconfigure(self.study_menu_index, label="结束学习")
            self.say("二十五分钟。坐好，别让我看到你分心。", 4500)
        else:
            self.study_mode = False
            self.menu.entryconfigure(self.study_menu_index, label="开始学习")
            self.say("五分钟。起来走走，别坐在那里装死。", 4500)

        self._render_timer()
        self.timer_job = self.root.after(250, self._timer_tick)

    def _timer_tick(self) -> None:
        self.timer_job = None
        if not self.timer_mode or self.timer_paused:
            return
        self.timer_remaining = max(0, math.ceil(self.timer_end - time.monotonic()))
        self._render_timer()
        if self.timer_remaining <= 0:
            self._finish_timer()
            return
        self.timer_job = self.root.after(250, self._timer_tick)

    def _render_timer(self) -> None:
        self.canvas.delete("timer")
        if not self.timer_mode:
            return
        minutes, seconds = divmod(self.timer_remaining, 60)
        label = f"{minutes:02d}:{seconds:02d}"
        is_focus = self.timer_mode == "focus"
        panel_fill = "#26344d" if is_focus else "#e7edf6"
        text_fill = "#f3f6fb" if is_focus else "#354762"
        outline = "#7185a7"
        self._rounded_rect(
            7,
            75,
            76,
            99,
            9,
            fill="#182238",
            outline="",
            tags="timer",
        )
        self._rounded_rect(
            5,
            72,
            74,
            96,
            9,
            fill=panel_fill,
            outline=outline,
            width=1,
            tags="timer",
        )
        self.canvas.create_text(
            39,
            84,
            text=label,
            fill=text_fill,
            font=("Segoe UI Semibold", 9),
            tags="timer",
        )

    def toggle_timer_pause(self) -> None:
        if not self.timer_mode:
            return
        self.last_interaction = time.monotonic()
        if self.timer_paused:
            self.timer_paused = False
            self.timer_end = time.monotonic() + self.timer_remaining
            self.timer_menu.entryconfigure(3, label="暂停")
            self.say("休息够了就继续。别磨蹭。", 3600)
            self.timer_job = self.root.after(250, self._timer_tick)
            return

        self.timer_remaining = max(0, math.ceil(self.timer_end - time.monotonic()))
        self.timer_paused = True
        if self.timer_job:
            self.root.after_cancel(self.timer_job)
            self.timer_job = None
        self.timer_menu.entryconfigure(3, label="继续")
        self._render_timer()
        self.say("……停下了？理由最好像样一点。", 3800)

    def stop_timer(self, silent: bool = False) -> None:
        if not self.timer_mode:
            return
        was_focus = self.timer_mode == "focus"
        self._reset_timer()
        if was_focus:
            self.study_mode = False
            self.menu.entryconfigure(self.study_menu_index, label="开始学习")
        if not silent:
            self.say("半途而废？……至少下次别再找借口。", 4200)

    def _finish_timer(self) -> None:
        finished_mode = self.timer_mode
        self._reset_timer()
        if finished_mode == "focus":
            self.study_mode = False
            self.menu.entryconfigure(self.study_menu_index, label="开始学习")
            self.say("时间到了。……做得还不算难看。", 5000)
        else:
            self.say("五分钟到了。回来，继续。", 4200)

    def _reset_timer(self) -> None:
        if self.timer_job:
            self.root.after_cancel(self.timer_job)
            self.timer_job = None
        self.timer_mode = None
        self.timer_end = 0.0
        self.timer_remaining = 0
        self.timer_paused = False
        self.canvas.delete("timer")
        if hasattr(self, "timer_menu"):
            self.timer_menu.entryconfigure(3, label="暂停", state="disabled")
            self.timer_menu.entryconfigure(4, state="disabled")

    def _study_check_in(self) -> None:
        if not self.study_mode:
            self.study_job = None
            return
        if not self.muted:
            self.say(random.choice([
                "哼，还没放弃啊，算你有点骨气。",
                "这种程度就想休息？还差得远呢。",
                "不错，勉强算是没在浪费时间。",
                "总算专心了一阵。……继续，别在这种地方停下。",
                "进度还过得去。别因为一句夸奖就得意忘形。",
            ]), 4600)
        self.study_job = self.root.after(STUDY_CHECK_IN_MS, self._study_check_in)

    def _schedule_long_sit_reminder(self) -> None:
        self.long_sit_job = self.root.after(LONG_SIT_MS, self._long_sit_reminder)

    def _long_sit_reminder(self) -> None:
        if not self.muted:
            self.say(random.choice([
                "喂，你是打算就这样坐上一辈子吗？",
                "一直坐着效率只会越来越差。去活动一下。",
            ]), 4800)
        self._schedule_long_sit_reminder()

    def contextual_line(self) -> str:
        hour = time.localtime().tm_hour
        if hour < 5:
            return "还不睡？……我也没资格说你。"
        if hour < 9:
            return "早。……就这样，别期待更多。"
        if hour >= 23:
            return "太晚了。剩下的明天再做。"
        return random.choice([
            "……今天很安静。这样就好。",
            "你那边，也到冬天了吗？",
            "别把声音开太大。会影响我。",
            "我只是稍微……不讨厌待在这里。",
            "该做的事做完了吗？……我只是随口问问。",
            "安静不是发呆。别混为一谈。",
        ])

    def _rounded_rect(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        radius: int,
        **options: object,
    ) -> int:
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1,
        ]
        return self.canvas.create_polygon(
            points,
            smooth=True,
            splinesteps=18,
            **options,
        )

    @staticmethod
    def _format_dialogue(text: str) -> str:
        """Put every complete sentence on its own line inside the bubble."""
        formatted = re.sub(r"([。！？!?]+)(?![。！？!?])", r"\1\n", text.strip())
        return formatted.rstrip("\n")

    def say(self, text: str, duration: int = 3600) -> None:
        if self.bubble_job:
            self.root.after_cancel(self.bubble_job)
        self.canvas.delete("bubble")
        sprite_top = self.sprite_base_y - self.sprite.height()
        bubble_y = max(24, sprite_top - 22)
        display_text = self._format_dialogue(text)
        text_id = self.canvas.create_text(
            self.width // 2,
            bubble_y,
            text=display_text,
            width=250,
            fill="#1d2940",
            font=("Microsoft YaHei UI", 9),
            justify="center",
            tags="bubble",
        )
        x1, y1, x2, y2 = self.canvas.bbox(text_id)
        pad_x, pad_y = 15, 8
        tip_bottom = y2 + pad_y + 8
        desired_tip_bottom = sprite_top - 3
        if tip_bottom > desired_tip_bottom:
            self.canvas.move(text_id, 0, desired_tip_bottom - tip_bottom)
            x1, y1, x2, y2 = self.canvas.bbox(text_id)
        left, top = x1 - pad_x, y1 - pad_y
        right, bottom = x2 + pad_x, y2 + pad_y
        center = self.width // 2

        shadow_id = self._rounded_rect(
            left + 2,
            top + 3,
            right + 2,
            bottom + 3,
            11,
            fill="#26344d",
            outline="",
            tags="bubble",
        )
        tail_shadow_id = self.canvas.create_polygon(
            center - 5,
            bottom + 1,
            center + 7,
            bottom + 1,
            center + 2,
            bottom + 11,
            fill="#26344d",
            outline="",
            tags="bubble",
        )
        tail_id = self.canvas.create_polygon(
            center - 5,
            bottom - 2,
            center + 5,
            bottom - 2,
            center,
            bottom + 8,
            fill="#eff3f8",
            outline="#6f83a6",
            width=1,
            tags="bubble",
        )
        panel_id = self._rounded_rect(
            left,
            top,
            right,
            bottom,
            11,
            fill="#eff3f8",
            outline="#6f83a6",
            width=1,
            tags="bubble",
        )
        accent_id = self.canvas.create_line(
            left + 12,
            top + 1,
            right - 12,
            top + 1,
            fill="#a8b8d1",
            width=1,
            tags="bubble",
        )
        for item_id in (shadow_id, tail_shadow_id, tail_id, panel_id, accent_id):
            self.canvas.tag_lower(item_id, text_id)
        self.bubble_job = self.root.after(duration, self.hide_bubble)

    def hide_bubble(self) -> None:
        self.canvas.delete("bubble")
        self.bubble_job = None

    def _schedule_idle(self, first: bool = False) -> None:
        delay = 18000 if first else random.randint(65000, 115000)
        self.idle_job = self.root.after(delay, self._idle_reaction)

    def _idle_reaction(self) -> None:
        if not self.muted and time.monotonic() - self.last_interaction > 16:
            self.say(self.contextual_line(), 4200)
        self._schedule_idle()

    def _animate(self) -> None:
        self.animation_tick += 1
        # Barely perceptible breathing; Kazusa should feel composed, not bouncy.
        offset = round(math.sin(self.animation_tick / 10) * 1.3)
        self.canvas.coords(self.sprite_id, self.width // 2, self.sprite_base_y + offset)
        self.root.after(90, self._animate)

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    if acquire_single_instance():
        try:
            KazusaPet().run()
        finally:
            release_single_instance()
