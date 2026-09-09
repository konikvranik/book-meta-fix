#!/usr/bin/env python3
"""Palette proposal for bmf CLI progress bars — iteration BEFORE touching cli.py.

Run from the repo root:

	.venv/bin/python scripts/progressbar_palette.py

Base: a brightness ladder (this terminal separates bars mainly by luminance;
blue-channel differences are weak — see the merged ANSI groups reported for
the strip below). On top of that, manual overrides per user feedback:
ACP -> desaturated blue-green, Rescanning -> purple, Cross-checking -> solid
blue (away from gold/orange), Clearing -> red (not orange/yellow/green).
"""
from __future__ import annotations

import os
import time

from rich.console import Console
from rich.progress import BarColumn, Progress, ProgressBar, SpinnerColumn, Task, TextColumn, TimeRemainingColumn

# (caption, task label, hex) — display order interleaves brightness so
# neighbouring rows differ strongly; rows are numbered for easy feedback.
SAMPLES: list[tuple[str, str, str]] = [
	("scan fáze — všechny příkazy", "Reading library", "#f6cd53"),        # gold, L=205
	("analyze — stahování ACP agenta", "Downloading the ACP agent", "#8fb6b8"),  # desat. teal, L=174
	("analyze — zpracování", "Analysing", "#459df5"),                      # azure, L=145
	("report", "Classifying", "#fdf6d4"),                                  # pale cream, L=245
	("apply", "Applying", "#1ef341"),                                      # vivid green, L=185
	("epubgen", "Generating EPUBs", "#078667"),                            # dark teal-green, L=105
	("crosscheck — 1. bar", "Cross-checking formats", "#3b5fd9"),          # blue, L=96
	("crosscheck — 2. bar (karanténa)", "Quarantining rogues", "#b60def"),  # violet-magenta, L=65
	("clean", "Cleaning", "#e9680c"),                                      # orange, L=125
	("abs-rescan — 1. bar", "Clearing broken covers", "#e83b3b"),          # red, L=96
	("abs-rescan — 2. bar", "Rescanning ABS items", "#5d16f2"),            # night-sky indigo-violet, L=53, sat ~0.83
]

TOTAL = 5000
FILLED = 2847  # 57 % — all comparison bars at the same fill level
STEPS = 40
STEP_SEC = 0.015  # ~0.6 s per animated bar


class _TaskBarColumn(BarColumn):
	"""BarColumn with per-task colours — the override analyze will need."""

	def render(self, task: Task) -> ProgressBar:
		override = task.fields.get("bar_style")
		if not override:
			return super().render(task)
		original = (self.complete_style, self.finished_style, self.pulse_style)
		self.complete_style = self.finished_style = self.pulse_style = override
		try:
			return super().render(task)
		finally:
			self.complete_style, self.finished_style, self.pulse_style = original


def _progress(console: Console, colour: str) -> Progress:
	return Progress(
		SpinnerColumn(),
		TextColumn("[progress.description]{task.description}"),
		_TaskBarColumn(complete_style=colour, finished_style=colour, pulse_style=colour),
		TextColumn("{task.completed}/{task.total}"),
		TimeRemainingColumn(),
		console=console,
	)


def main() -> None:
	console = Console()
	console.print("[bold]bmf — návrh palety progressbarů[/bold]")
	console.print()
	console.print(
		f"[dim]terminal: color_system={console.color_system!r}  "
		f"TERM={os.environ.get('TERM', '?')}  "
		f"COLORTERM={os.environ.get('COLORTERM', '-')}[/dim]"
	)
	console.print()

	# Autodiagnostika: všech 16 základních ANSI barev, seskupených po
	# rodinách (základní + bright vedle sebe) — na přeskočku se to špatně
	# posuzovalo. Která čísla splývají, ta v paletě rozlišit nepůjdou.
	console.print("[bold]A) 16 základních barev terminálu[/bold] (rodiny u sebe; napište, která čísla splývají):")
	console.print()
	ansi16 = [
		(0, "black"), (8, "bright_black"), (7, "white"), (15, "bright_white"),
		(1, "red"), (9, "bright_red"), (3, "yellow"), (11, "bright_yellow"),
		(2, "green"), (10, "bright_green"), (6, "cyan"), (14, "bright_cyan"),
		(4, "blue"), (12, "bright_blue"), (5, "magenta"), (13, "bright_magenta"),
	]
	for i, name in ansi16:
		block = "[on color(" + str(i) + ")]      [/on color(" + str(i) + ")]"
		console.print(f"  {i:2} {block} [dim]{name}[/dim]")
	console.print()

	# Static comparison — every bar at 57 %, numbered.
	for i, (caption, label, colour) in enumerate(SAMPLES, start=1):
		with _progress(console, colour) as p:
			task_id = p.add_task(label, total=TOTAL)
			p.update(task_id, completed=FILLED)
		console.print(f"  [dim]↑ {i:2}. {caption} — {colour}[/dim]")
		console.print()

	# Live demo: analyze scan phase — two tasks, one Progress, per-task colours.
	console.print("[dim]živě: analyze — scan fáze (Reading library × ACP download v jednom Progress):[/dim]")
	scan_colour = SAMPLES[0][2]
	acp_colour = SAMPLES[1][2]
	with _progress(console, scan_colour) as p:
		scan = p.add_task("Reading library", total=TOTAL)
		acp = p.add_task("Downloading the ACP agent", total=TOTAL, bar_style=acp_colour)
		for i in range(1, STEPS + 1):
			p.update(scan, completed=TOTAL * i // STEPS)
			p.update(acp, completed=TOTAL * (i * 2) // (STEPS * 3))  # download jde pomaleji
			time.sleep(STEP_SEC)
	console.print()


if __name__ == "__main__":
	main()
