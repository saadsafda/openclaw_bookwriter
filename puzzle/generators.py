"""Puzzle grid construction and 300 DPI rendering.

This module replaces the two third-party services the spec flagged as
"automation decision needed":

  * mazepuzzlemaker.com (Section 2) — sells only 3/7/14 day memberships, so
    every book had to be batched into one membership window.
  * Discovery Education's criss-cross tool (Section 7).

Everything here is in-house: a perfect maze via randomized DFS, a word-search
placer, and a criss-cross crossword layout, each rendered to grayscale PNG at
6x9 in / 300 DPI with a matching solution image for the answer key.

Rendering uses Pillow only — already a dependency via openclaw_image_maker.
Builds are seeded, so a given book id always regenerates identical puzzles.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from PIL import Image, ImageDraw, ImageFont

from print_hygiene import sanitize_for_print

from .engine import (
    PAGE_H_IN,
    PAGE_W_IN,
    PRINT_DPI,
    Crossword,
    CrosswordEntry,
    PuzzleError,
    WordSearch,
    normalize_word,
)

# Full-bleed page in pixels at print resolution.
PAGE_W_PX = int(PAGE_W_IN * PRINT_DPI)   # 1800
PAGE_H_PX = int(PAGE_H_IN * PRINT_DPI)   # 2700

# Margin inside which all puzzle art is drawn, leaving room for the formatter's
# gutters and page furniture.
MARGIN_PX = int(0.75 * PRINT_DPI)

WHITE = 255
BLACK = 0
GRAY = 128

# Eight directions for word-search placement: E, SE, S, SW, W, NW, N, NE.
_DIRECTIONS = ((0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1), (-1, 0), (-1, 1))

# Easier books get forward-only words; harder ones get all eight.
_DIFFICULTY_DIRECTIONS = {
    "easy": ((0, 1), (1, 0)),
    "medium": ((0, 1), (1, 0), (1, 1), (-1, 1)),
    "hard": _DIRECTIONS,
}

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


# --------------------------------------------------------------------------
# Fonts
# --------------------------------------------------------------------------

_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _load_font(size: int) -> ImageFont.ImageFont:
    """Best available TrueType face, falling back to Pillow's bitmap font.

    The fallback is unscalable and will look small at 300 DPI, but a missing
    system font must never fail a build.
    """
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return right - left, bottom - top


def _draw_centered(draw: ImageDraw.ImageDraw, cx: int, cy: int, text: str, font, fill=BLACK) -> None:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text((cx - (right + left) / 2, cy - (bottom + top) / 2), text, font=font, fill=fill)


def _new_page() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("L", (PAGE_W_PX, PAGE_H_PX), WHITE)
    return img, ImageDraw.Draw(img)


def _save(img: Image.Image, path: Path) -> Path:
    """Save grayscale at 300 DPI, as every asset in the spec requires.

    The sanitize pass is what guarantees the DPI is exactly 300 and that no
    metadata rides along; Pillow's own save is not sufficient on either count.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(path), format="PNG", dpi=(PRINT_DPI, PRINT_DPI), optimize=True)
    sanitize_for_print(path, PRINT_DPI)
    return path


def _draw_title(draw: ImageDraw.ImageDraw, title: str, subtitle: str = "") -> int:
    """Title block at the top of a puzzle page. Returns the y to start art at."""
    y = MARGIN_PX
    if title:
        font = _load_font(72)
        w, h = _text_size(draw, title, font)
        draw.text(((PAGE_W_PX - w) / 2, y), title, font=font, fill=BLACK)
        y += h + 26
    if subtitle:
        font = _load_font(40)
        w, h = _text_size(draw, subtitle, font)
        draw.text(((PAGE_W_PX - w) / 2, y), subtitle, font=font, fill=GRAY)
        y += h + 20
    return y + 30


# ==========================================================================
# Section 2 — Mazes
# ==========================================================================

@dataclass
class MazeGrid:
    cols: int
    rows: int
    # Wall flags per cell: N, E, S, W
    walls: list[list[list[bool]]]
    solution: list[tuple[int, int]]

    def carved(self, r: int, c: int, side: int) -> bool:
        return not self.walls[r][c][side]


_N, _E, _S, _W = 0, 1, 2, 3
_OPPOSITE = {_N: _S, _S: _N, _E: _W, _W: _E}
_DELTA = {_N: (-1, 0), _S: (1, 0), _E: (0, 1), _W: (0, -1)}


def generate_maze(cols: int, rows: int, seed: int) -> MazeGrid:
    """Perfect maze by randomized depth-first search.

    A perfect maze has exactly one path between any two cells, so the solution
    from entrance to exit is unique — required for a clean answer key.
    """
    if cols < 2 or rows < 2:
        raise PuzzleError("Maze must be at least 2x2.")

    rng = random.Random(seed)
    walls = [[[True, True, True, True] for _ in range(cols)] for _ in range(rows)]
    visited = [[False] * cols for _ in range(rows)]

    # Iterative DFS — recursion would blow the stack on large mazes.
    stack = [(0, 0)]
    visited[0][0] = True
    while stack:
        r, c = stack[-1]
        options = []
        for side, (dr, dc) in _DELTA.items():
            nr, nc = r + dr, c + dc
            if 0 <= nr < rows and 0 <= nc < cols and not visited[nr][nc]:
                options.append((side, nr, nc))
        if not options:
            stack.pop()
            continue
        side, nr, nc = rng.choice(options)
        walls[r][c][side] = False
        walls[nr][nc][_OPPOSITE[side]] = False
        visited[nr][nc] = True
        stack.append((nr, nc))

    # Entrance top-left, exit bottom-right.
    walls[0][0][_W] = False
    walls[rows - 1][cols - 1][_E] = False

    solution = _solve_maze(walls, cols, rows)
    return MazeGrid(cols=cols, rows=rows, walls=walls, solution=solution)


def _solve_maze(walls, cols: int, rows: int) -> list[tuple[int, int]]:
    """Path from (0,0) to (rows-1, cols-1) via DFS with backtracking."""
    start, goal = (0, 0), (rows - 1, cols - 1)
    stack = [start]
    seen = {start}
    parent: dict[tuple[int, int], tuple[int, int]] = {}

    while stack:
        cur = stack.pop()
        if cur == goal:
            break
        r, c = cur
        for side, (dr, dc) in _DELTA.items():
            if walls[r][c][side]:
                continue
            nxt = (r + dr, c + dc)
            if not (0 <= nxt[0] < rows and 0 <= nxt[1] < cols) or nxt in seen:
                continue
            seen.add(nxt)
            parent[nxt] = cur
            stack.append(nxt)

    if goal not in parent and goal != start:
        return []
    path = [goal]
    while path[-1] != start:
        path.append(parent[path[-1]])
    path.reverse()
    return path


def render_maze(
    maze: MazeGrid, path: Path, *, title: str = "", subtitle: str = "", solution: bool = False
) -> Path:
    img, draw = _new_page()
    top = _draw_title(draw, title, subtitle)

    avail_w = PAGE_W_PX - 2 * MARGIN_PX
    avail_h = PAGE_H_PX - top - MARGIN_PX
    cell = min(avail_w // maze.cols, avail_h // maze.rows)
    grid_w, grid_h = cell * maze.cols, cell * maze.rows
    ox = (PAGE_W_PX - grid_w) // 2
    oy = top + (avail_h - grid_h) // 2

    # Solution first, so walls draw over it cleanly.
    if solution and maze.solution:
        pts = [
            (ox + c * cell + cell // 2, oy + r * cell + cell // 2)
            for r, c in maze.solution
        ]
        # Extend through the entrance and exit openings.
        pts.insert(0, (ox - cell // 2, oy + cell // 2))
        pts.append((ox + grid_w + cell // 2, oy + grid_h - cell // 2))
        draw.line(pts, fill=GRAY, width=max(4, cell // 6), joint="curve")

    wall_w = max(3, cell // 12)
    for r in range(maze.rows):
        for c in range(maze.cols):
            x0, y0 = ox + c * cell, oy + r * cell
            x1, y1 = x0 + cell, y0 + cell
            w = maze.walls[r][c]
            if w[_N]:
                draw.line([(x0, y0), (x1, y0)], fill=BLACK, width=wall_w)
            if w[_S]:
                draw.line([(x0, y1), (x1, y1)], fill=BLACK, width=wall_w)
            if w[_W]:
                draw.line([(x0, y0), (x0, y1)], fill=BLACK, width=wall_w)
            if w[_E]:
                draw.line([(x1, y0), (x1, y1)], fill=BLACK, width=wall_w)

    label = _load_font(max(28, cell // 2))
    draw.text((ox - cell, oy + cell // 4), "START", font=label, fill=BLACK)
    end_w, _h = _text_size(draw, "END", label)
    draw.text((ox + grid_w + cell // 4, oy + grid_h - cell), "END", font=label, fill=BLACK)

    return _save(img, path)


# ==========================================================================
# Section 4 — Word searches
# ==========================================================================

def build_word_search(
    words: Iterable[str], size: int, seed: int, difficulty: str = "medium"
) -> tuple[list[list[str]], dict[str, dict[str, int]]]:
    """Place words in a square grid, then fill the gaps with random letters.

    Returns the grid and the placements needed to draw the solution overlay.
    Raises if a word cannot be placed after exhausting its candidate slots.
    """
    clean = [normalize_word(w) for w in words]
    clean = [w for w in clean if w]
    if not clean:
        raise PuzzleError("No usable words for the word search.")
    longest = max(len(w) for w in clean)
    if longest > size:
        raise PuzzleError(f"Word {longest} letters long does not fit a {size}x{size} grid.")

    rng = random.Random(seed)
    directions = _DIFFICULTY_DIRECTIONS.get(difficulty, _DIFFICULTY_DIRECTIONS["medium"])
    grid: list[list[Optional[str]]] = [[None] * size for _ in range(size)]
    placements: dict[str, dict[str, int]] = {}

    # Longest first: long words have the fewest legal positions, so placing
    # them while the grid is empty avoids dead ends.
    for word in sorted(clean, key=len, reverse=True):
        candidates = []
        for dr, dc in directions:
            max_r = size - (len(word) - 1) * dr if dr > 0 else size
            min_r = (len(word) - 1) * -dr if dr < 0 else 0
            max_c = size - (len(word) - 1) * dc if dc > 0 else size
            min_c = (len(word) - 1) * -dc if dc < 0 else 0
            for r in range(max(0, min_r), max(0, max_r)):
                for c in range(max(0, min_c), max(0, max_c)):
                    candidates.append((r, c, dr, dc))
        rng.shuffle(candidates)

        placed = False
        for r, c, dr, dc in candidates:
            cells = [(r + dr * i, c + dc * i) for i in range(len(word))]
            if any(not (0 <= rr < size and 0 <= cc < size) for rr, cc in cells):
                continue
            # Overlaps are allowed only where the letters already match.
            if any(grid[rr][cc] is not None and grid[rr][cc] != word[i]
                   for i, (rr, cc) in enumerate(cells)):
                continue
            for i, (rr, cc) in enumerate(cells):
                grid[rr][cc] = word[i]
            placements[word] = {"row": r, "col": c, "dr": dr, "dc": dc}
            placed = True
            break

        if not placed:
            raise PuzzleError(f"Could not place '{word}' in a {size}x{size} grid.")

    for r in range(size):
        for c in range(size):
            if grid[r][c] is None:
                grid[r][c] = rng.choice(_ALPHABET)

    return [[str(ch) for ch in row] for row in grid], placements


def render_word_search(
    puzzle: WordSearch, path: Path, *, title: str = "", solution: bool = False
) -> Path:
    img, draw = _new_page()
    heading = title or puzzle.title
    top = _draw_title(draw, heading, "Find all 9 hidden words." if not solution else "Solution")

    size = len(puzzle.grid)
    if size == 0:
        raise PuzzleError("Word search has no grid to render.")

    # Reserve space under the grid for the word list (3 columns).
    word_rows = (len(puzzle.words) + 2) // 3
    list_h = word_rows * 70 + 60
    avail_w = PAGE_W_PX - 2 * MARGIN_PX
    avail_h = PAGE_H_PX - top - MARGIN_PX - list_h
    cell = min(avail_w // size, avail_h // size)
    grid_w = cell * size
    ox = (PAGE_W_PX - grid_w) // 2
    # Center the grid+wordlist block in the space below the title, so the page
    # reads balanced rather than top-heavy.
    oy = top + max(0, (avail_h - grid_w) // 2)

    if solution:
        # Draw a rounded capsule behind each placed word.
        for word, p in puzzle.placements.items():
            r, c, dr, dc = p["row"], p["col"], p["dr"], p["dc"]
            x0 = ox + c * cell + cell // 2
            y0 = oy + r * cell + cell // 2
            x1 = ox + (c + dc * (len(word) - 1)) * cell + cell // 2
            y1 = oy + (r + dr * (len(word) - 1)) * cell + cell // 2
            draw.line([(x0, y0), (x1, y1)], fill=200, width=int(cell * 0.82))
            for x, y in ((x0, y0), (x1, y1)):
                rad = int(cell * 0.41)
                draw.ellipse([x - rad, y - rad, x + rad, y + rad], fill=200)

    font = _load_font(int(cell * 0.62))
    line_w = max(2, cell // 22)
    for r in range(size):
        for c in range(size):
            x, y = ox + c * cell, oy + r * cell
            draw.rectangle([x, y, x + cell, y + cell], outline=180, width=line_w)
            _draw_centered(draw, x + cell // 2, y + cell // 2, puzzle.grid[r][c], font)

    # Word list under the grid.
    ly = oy + grid_w + 55
    wfont = _load_font(46)
    col_w = (PAGE_W_PX - 2 * MARGIN_PX) // 3
    box = int(wfont.size * 0.62) if hasattr(wfont, "size") else 28
    for i, word in enumerate(puzzle.words):
        col, row = i % 3, i // 3
        x = MARGIN_PX + col * col_w
        y = ly + row * 70
        # An outlined square rather than a glyph — Arial has no checkbox
        # character, and a missing glyph renders as tofu.
        draw.rectangle([x, y + 6, x + box, y + 6 + box], outline=BLACK, width=3)
        draw.text((x + box + 18, y), word, font=wfont, fill=BLACK)

    return _save(img, path)


# ==========================================================================
# Section 7 — Crosswords (criss-cross layout)
# ==========================================================================

def build_crossword(entries: list[CrosswordEntry], seed: int) -> tuple[list[list[str]], dict[str, int]]:
    """Criss-cross layout: place the longest word, then interlock the rest on
    shared letters. Same style of grid as the Discovery Education tool.

    Mutates each entry with its row/col/direction/number and returns the
    trimmed grid plus the clue numbering keyed by "row,col".
    """
    usable = [e for e in entries if normalize_word(e.word)]
    if not usable:
        raise PuzzleError("No usable words for the crossword.")

    for e in usable:
        e.word = normalize_word(e.word)

    rng = random.Random(seed)
    ordered = sorted(usable, key=lambda e: len(e.word), reverse=True)

    # Work on a sparse map so the grid can grow in any direction.
    cells: dict[tuple[int, int], str] = {}
    placed: list[CrosswordEntry] = []

    first = ordered[0]
    for i, ch in enumerate(first.word):
        cells[(0, i)] = ch
    first.row, first.col, first.direction = 0, 0, "across"
    placed.append(first)

    def fits(word: str, r: int, c: int, dr: int, dc: int) -> bool:
        """True when the word can occupy these cells without creating an
        accidental adjacent word."""
        for i, ch in enumerate(word):
            rr, cc = r + dr * i, c + dc * i
            existing = cells.get((rr, cc))
            if existing is not None:
                if existing != ch:
                    return False
                continue
            # Cells beside a newly-filled square must stay empty, or two words
            # would run together side by side.
            if dr == 0:
                if cells.get((rr - 1, cc)) is not None or cells.get((rr + 1, cc)) is not None:
                    return False
            else:
                if cells.get((rr, cc - 1)) is not None or cells.get((rr, cc + 1)) is not None:
                    return False
        # The squares just before and after the word must be empty, so it does
        # not extend an existing word.
        before = (r - dr, c - dc)
        after = (r + dr * len(word), c + dc * len(word))
        return cells.get(before) is None and cells.get(after) is None

    for entry in ordered[1:]:
        word = entry.word
        options: list[tuple[int, int, int, int, int]] = []
        for i, ch in enumerate(word):
            for (rr, cc), existing in cells.items():
                if existing != ch:
                    continue
                # Cross the existing word perpendicularly.
                for dr, dc in ((1, 0), (0, 1)):
                    r0, c0 = rr - dr * i, cc - dc * i
                    if fits(word, r0, c0, dr, dc):
                        # Prefer placements that stay compact.
                        score = abs(r0) + abs(c0)
                        options.append((score, r0, c0, dr, dc))
        if not options:
            continue
        options.sort(key=lambda o: (o[0], rng.random()))
        _score, r0, c0, dr, dc = options[0]
        for i, ch in enumerate(word):
            cells[(r0 + dr * i, c0 + dc * i)] = ch
        entry.row, entry.col = r0, c0
        entry.direction = "down" if dr else "across"
        placed.append(entry)

    unplaced = [e.word for e in ordered if e not in placed]
    if unplaced:
        raise PuzzleError(
            "Could not interlock these word(s): " + ", ".join(unplaced)
        )

    # Normalize coordinates so the grid starts at (0,0).
    min_r = min(r for r, _ in cells)
    min_c = min(c for _, c in cells)
    max_r = max(r for r, _ in cells)
    max_c = max(c for _, c in cells)
    height, width = max_r - min_r + 1, max_c - min_c + 1

    grid = [["" for _ in range(width)] for _ in range(height)]
    for (r, c), ch in cells.items():
        grid[r - min_r][c - min_c] = ch
    for e in placed:
        e.row -= min_r
        e.col -= min_c

    # Number the entries in reading order, sharing a number where an across and
    # a down entry start on the same square.
    numbers: dict[str, int] = {}
    counter = 0
    for e in sorted(placed, key=lambda x: (x.row, x.col)):
        key = f"{e.row},{e.col}"
        if key not in numbers:
            counter += 1
            numbers[key] = counter
        e.number = numbers[key]

    return grid, numbers


def render_crossword(
    puzzle: Crossword, path: Path, *, title: str = "", solution: bool = False
) -> Path:
    img, draw = _new_page()
    heading = title or puzzle.title
    top = _draw_title(draw, heading, "Solution" if solution else "")

    grid = puzzle.grid
    if not grid or not grid[0]:
        raise PuzzleError("Crossword has no grid to render.")
    rows, cols = len(grid), len(grid[0])

    across = [e for e in puzzle.entries if e.direction == "across"]
    down = [e for e in puzzle.entries if e.direction == "down"]
    clue_lines = len(across) + len(down) + 2
    clue_h = clue_lines * 58 + 60

    avail_w = PAGE_W_PX - 2 * MARGIN_PX
    avail_h = PAGE_H_PX - top - MARGIN_PX - clue_h
    cell = max(40, min(avail_w // cols, avail_h // rows))
    grid_w, grid_h = cell * cols, cell * rows
    ox = (PAGE_W_PX - grid_w) // 2
    oy = top + max(0, (avail_h - grid_h) // 4)

    letter_font = _load_font(int(cell * 0.6))
    num_font = _load_font(max(18, int(cell * 0.26)))
    border = max(3, cell // 18)

    for r in range(rows):
        for c in range(cols):
            if not grid[r][c]:
                continue
            x, y = ox + c * cell, oy + r * cell
            draw.rectangle([x, y, x + cell, y + cell], fill=WHITE, outline=BLACK, width=border)
            num = puzzle.numbers.get(f"{r},{c}")
            if num:
                draw.text((x + cell * 0.08, y + cell * 0.05), str(num), font=num_font, fill=BLACK)
            if solution:
                _draw_centered(draw, x + cell // 2, y + cell // 2 + int(cell * 0.06),
                               grid[r][c], letter_font)

    # Clues below the grid.
    y = oy + grid_h + 60
    head_font = _load_font(48)
    clue_font = _load_font(40)
    for label, group in (("ACROSS", across), ("DOWN", down)):
        if not group:
            continue
        draw.text((MARGIN_PX, y), label, font=head_font, fill=BLACK)
        y += 62
        for e in sorted(group, key=lambda x: x.number):
            draw.text((MARGIN_PX + 20, y), f"{e.number}. {e.clue}", font=clue_font, fill=BLACK)
            y += 56
        y += 18

    return _save(img, path)
