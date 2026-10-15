"""Thai checkers (หมากฮอส): the rules, legal moves and a computer player.

The board is a string of 64 squares, row by row from the top. Pieces stand on the dark squares
((row + column) is odd): "w" and "W" are White's men and kings (ฮอส), "b" and "B" Black's, "." is
empty. White starts on the two bottom rows and moves first; Black starts on the two top rows.

Thai rules used here:
- Men move one square diagonally forward, and capture forward only, by jumping an enemy piece
  next to them onto the empty square behind it.
- A man that reaches the far row becomes a king (ฮอส). Kings move any distance diagonally. A king
  captures by flying along a diagonal to an enemy piece and must land on the square right behind it.
- Capturing is compulsory, and a capture continues while the piece can capture again; captured
  pieces come off at once. A man that becomes a king during a capture stops there.
- You lose when it's your turn and you have no pieces or no legal move. The game is a draw after
  DRAW_PLIES moves in a row by kings with no capture.
"""

import random
import time

SIZE = 8
EMPTY = "."
DIAGONALS = ((-1, -1), (-1, 1), (1, -1), (1, 1))
FORWARD = {"w": -1, "b": 1}
LAST_ROW = {"w": 0, "b": SIZE - 1}
DRAW_PLIES = 40
LEVELS = {"easy": "Easy", "medium": "Medium", "hard": "Hard"}
MAN, KING = 10, 28


def new_board():
    squares = []
    for row in range(SIZE):
        for col in range(SIZE):
            dark = (row + col) % 2 == 1
            squares.append("b" if dark and row < 2 else "w" if dark and row >= SIZE - 2 else EMPTY)
    return "".join(squares)


def new_state():
    return {"board": new_board(), "turn": "w", "quiet": 0, "last": None, "plies": 0}


def side_of(piece):
    return "w" if piece in "wW" else "b" if piece in "bB" else None


def other(side):
    return "b" if side == "w" else "w"


def _on(row, col):
    return 0 <= row < SIZE and 0 <= col < SIZE


def _jumps(board, row, col, piece, side):
    """Every complete capture sequence from (row, col): a list of (landing squares, captured squares)."""
    enemy = other(side)
    king = piece.isupper()
    sequences = []
    for dr, dc in (DIAGONALS if king else ((FORWARD[side], -1), (FORWARD[side], 1))):
        r, c = row + dr, col + dc
        if king:
            while _on(r, c) and board[r * SIZE + c] == EMPTY:
                r, c = r + dr, c + dc
        if not _on(r, c) or side_of(board[r * SIZE + c]) != enemy:
            continue
        land_r, land_c = r + dr, c + dc
        if not _on(land_r, land_c) or board[land_r * SIZE + land_c] != EMPTY:
            continue
        land, taken = land_r * SIZE + land_c, r * SIZE + c
        promoted = not king and land_r == LAST_ROW[side]
        moved = piece.upper() if promoted else piece
        after = list(board)
        after[row * SIZE + col] = EMPTY
        after[taken] = EMPTY
        after[land] = moved
        more = [] if promoted else _jumps("".join(after), land_r, land_c, moved, side)
        if more:
            sequences += [([land] + path, [taken] + captured) for path, captured in more]
        else:
            sequences.append(([land], [taken]))
    return sequences


def legal_moves(board, side):
    """The moves this side may make: captures only, if there are any. Each is a dict with the
    squares visited ("path", starting square first) and the squares captured."""
    captures, steps = [], []
    for square, piece in enumerate(board):
        if side_of(piece) != side:
            continue
        row, col = divmod(square, SIZE)
        for path, captured in _jumps(board, row, col, piece, side):
            captures.append({"path": [square] + path, "captured": captured})
        if captures:
            continue  # Once a capture exists, quiet moves don't matter.
        for dr, dc in (DIAGONALS if piece.isupper() else ((FORWARD[side], -1), (FORWARD[side], 1))):
            r, c = row + dr, col + dc
            while _on(r, c) and board[r * SIZE + c] == EMPTY:
                steps.append({"path": [square, r * SIZE + c], "captured": []})
                if not piece.isupper():
                    break
                r, c = r + dr, c + dc
    return captures or steps


def apply_move(board, move, side):
    """The board after this move."""
    squares = list(board)
    start, end = move["path"][0], move["path"][-1]
    piece = squares[start]
    squares[start] = EMPTY
    for taken in move["captured"]:
        squares[taken] = EMPTY
    if piece.islower() and end // SIZE == LAST_ROW[side]:
        piece = piece.upper()
    squares[end] = piece
    return "".join(squares)


def play(state, path):
    """Plays the move with exactly these squares for the side to move. Returns the new state and the
    result: None while the game goes on, else "w", "b" (the winner) or "draw". Raises ValueError for
    a move that isn't legal."""
    side = state["turn"]
    move = next((m for m in legal_moves(state["board"], side) if m["path"] == list(path)), None)
    if move is None:
        raise ValueError("That move isn't allowed.")
    moved_king = state["board"][move["path"][0]].isupper()
    board = apply_move(state["board"], move, side)
    quiet = state["quiet"] + 1 if moved_king and not move["captured"] else 0
    new = {"board": board, "turn": other(side), "quiet": quiet, "last": move, "plies": state.get("plies", 0) + 1}
    return new, result(new)


def result(state):
    if not legal_moves(state["board"], state["turn"]):
        return other(state["turn"])
    if state["quiet"] >= DRAW_PLIES:
        return "draw"
    return None


def counts(board):
    return {side: sum(1 for piece in board if side_of(piece) == side) for side in "wb"}


# ---------------------------------------------------------------- computer player

def evaluate(board, side):
    """How good the board is for this side: material, plus men that have moved up and kept the centre."""
    score = 0
    for square, piece in enumerate(board):
        owner = side_of(piece)
        if owner is None:
            continue
        row, col = divmod(square, SIZE)
        if piece.isupper():
            value = KING
        else:
            advanced = (SIZE - 1 - row) if owner == "w" else row
            value = MAN + advanced * 0.6 + (0.4 if 2 <= col <= 5 else 0)
            if row == (SIZE - 1 if owner == "w" else 0):
                value += 0.8  # Men left on the back row guard against kings.
        score += value if owner == side else -value
    return score


class _OutOfTime(Exception):
    pass


def _search(board, side, depth, alpha, beta, deadline, extra=4):
    if deadline and time.monotonic() > deadline:
        raise _OutOfTime
    moves = legal_moves(board, side)
    if not moves:
        return -1000 - depth  # Losing sooner is worse.
    if depth <= 0 and (not moves[0]["captured"] or extra <= 0):
        return evaluate(board, side)
    if depth <= 0:
        extra -= 1  # Keep following captures so a piece isn't left hanging at the horizon.
    moves.sort(key=lambda m: -len(m["captured"]))
    best = -10 ** 6
    for move in moves:
        score = -_search(apply_move(board, move, side), other(side), depth - 1, -beta, -alpha, deadline, extra)
        best = max(best, score)
        alpha = max(alpha, score)
        if alpha >= beta:
            break
    return best


def _ranked(board, side, depth, deadline=None):
    moves = legal_moves(board, side)
    scored = []
    for move in moves:
        score = -_search(apply_move(board, move, side), other(side), depth - 1, -10 ** 6, 10 ** 6, deadline)
        scored.append((score, move))
    return scored


def choose_move(state, level="medium", rng=None, think_time=1.5):
    """The computer's move for the side to move, at this level."""
    rng = rng or random.Random()
    board, side = state["board"], state["turn"]
    moves = legal_moves(board, side)
    if not moves:
        return None
    if len(moves) == 1:
        return moves[0]
    if level == "easy":
        if rng.random() < 0.6:
            return rng.choice(moves)
        scored = _ranked(board, side, 1)
    elif level == "medium":
        scored = _ranked(board, side, 3)
        if rng.random() < 0.15:  # Now and then, the second-best move.
            scored.sort(key=lambda item: -item[0])
            return scored[min(1, len(scored) - 1)][1]
    else:
        deadline = time.monotonic() + think_time
        scored = _ranked(board, side, 2)
        depth = 3
        while depth <= 12:
            try:
                scored = _ranked(board, side, depth, deadline)
            except _OutOfTime:
                break
            depth += 1
    best = max(score for score, _move in scored)
    return rng.choice([move for score, move in scored if score >= best - 1e-9])
