// Thai checkers board: draws the position, lets the player to move pick a move, and keeps the
// board up to date for both players and anyone watching. The server checks every move.
(function () {
  const root = document.getElementById("game");
  if (!root) return;
  const boardEl = document.getElementById("board");
  const statusEl = document.getElementById("status");
  const errorEl = document.getElementById("error");
  const betEl = document.getElementById("bet");
  const resignBtn = document.getElementById("resign");
  const againForm = document.getElementById("again");
  let game = JSON.parse(document.getElementById("game-initial").textContent);
  const startStatus = game.status;
  let partial = [];  // the squares picked so far for the move being made
  let busy = false;

  const flipped = () => game.you === "b";  // Black sees the board from their side.
  const side = (piece) => (piece === "w" || piece === "W" ? "w" : piece === "b" || piece === "B" ? "b" : null);
  const name = (s) => game.players[s] || "—";
  const colour = (s) => (s === "w" ? "White" : "Black");

  function candidates() {
    return game.moves.filter((path) => partial.every((square, i) => path[i] === square));
  }

  function render() {
    const lastSquares = new Set(game.last ? game.last.path : []);
    const captured = new Set(game.last ? game.last.captured : []);
    const options = candidates();
    const starts = new Set(game.moves.map((path) => path[0]));
    const next = new Set(partial.length ? options.map((path) => path[partial.length]).filter((s) => s !== undefined) : []);
    boardEl.innerHTML = "";
    for (let i = 0; i < 64; i++) {
      const square = flipped() ? 63 - i : i;
      const row = Math.floor(square / 8), col = square % 8;
      const cell = document.createElement("button");
      cell.type = "button";
      cell.className = "game-square " + ((row + col) % 2 ? "is-dark" : "is-light");
      cell.dataset.square = square;
      cell.setAttribute("role", "gridcell");
      const piece = game.board[square];
      if (lastSquares.has(square)) cell.classList.add("is-last");
      if (captured.has(square)) cell.classList.add("is-taken");
      if (partial.includes(square)) cell.classList.add("is-path");
      if (next.has(square)) cell.classList.add("is-target");
      if (!partial.length && starts.has(square)) cell.classList.add("is-movable");
      if (partial[0] === square) cell.classList.add("is-selected");
      if (piece !== ".") {
        const disc = document.createElement("span");
        disc.className = `game-piece is-${side(piece)}` + (piece === piece.toUpperCase() ? " is-king" : "");
        if (piece === piece.toUpperCase()) disc.textContent = "♛";
        cell.appendChild(disc);
      }
      const label = `${"abcdefgh"[col]}${8 - row}` + (piece !== "." ? `, ${colour(side(piece))}${piece === piece.toUpperCase() ? " king" : ""}` : "");
      cell.setAttribute("aria-label", label);
      if ((row + col) % 2 === 0 || !game.moves.length) cell.disabled = true;
      boardEl.appendChild(cell);
    }
    const top = flipped() ? "w" : "b", bottom = flipped() ? "b" : "w";
    for (const [id, s] of [["player-top", top], ["player-bottom", bottom]]) {
      const el = document.getElementById(id);
      el.innerHTML = "";
      const dot = document.createElement("span");
      dot.className = `game-dot is-${s}`;
      const who = document.createElement("strong");
      who.textContent = name(s) || "Waiting for a player…";
      el.append(dot, who);
      el.classList.toggle("is-turn", game.status === "playing" && game.turn === s);
      if (game.you === s) el.append(Object.assign(document.createElement("span"), { className: "mission-muted", textContent: " (you)" }));
    }
    document.getElementById("counts").textContent = `White ${game.counts.w} · Black ${game.counts.b}`;
    document.getElementById("plies").textContent = game.plies;
    if (game.bet) {
      betEl.hidden = false;
      betEl.textContent = `🪙 ${game.bet.toLocaleString()} points each · winner takes ${(2 * game.bet).toLocaleString()}`;
    }
    let text;
    if (busy) text = game.mode === "bot" ? "The computer is thinking…" : "Playing your move…";
    else if (game.status === "waiting") text = "Waiting for a second player to join.";
    else if (game.status === "playing") {
      if (game.moves.length) text = (game.must_capture ? "Your move: you must capture." : "Your move.")
        + (partial.length ? " Click the highlighted square to continue." : " Pick a piece.");
      else if (game.you) text = `${name(game.turn)} is thinking…`;
      else text = `${colour(game.turn)} to move (${name(game.turn)}). You're watching.`;
    } else text = game.result || "Game over.";
    statusEl.textContent = text;
    statusEl.classList.toggle("is-over", game.status === "finished" || game.status === "cancelled");
    resignBtn.hidden = !(game.status === "playing" && game.you);
    if (againForm) againForm.hidden = game.status !== "finished";
  }

  function showError(message) {
    errorEl.hidden = !message;
    errorEl.textContent = message || "";
  }

  async function send(url, body) {
    busy = true;
    showError("");
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": root.dataset.csrf, Accept: "application/json" },
        body: JSON.stringify(body || {}),
      });
      const data = await response.json();
      busy = false;
      if (!response.ok) {
        showError(data.error || "That didn't work.");
        await refresh(true);
      } else {
        update(data);
      }
    } catch (err) {
      busy = false;
      showError("Couldn't reach the server. Check the connection.");
      render();
    } finally {
      busy = false;
    }
  }

  function update(data) {
    if (data.status !== startStatus && startStatus === "waiting") {
      window.location.reload();  // Someone joined: show the full game page.
      return;
    }
    const changed = data.version !== game.version;
    game = data;
    if (changed) partial = [];
    render();
  }

  boardEl.addEventListener("click", (event) => {
    const cell = event.target.closest(".game-square");
    if (!cell || busy || !game.moves.length) return;
    const square = Number(cell.dataset.square);
    if (!partial.length || partial[0] === square || (game.moves.some((p) => p[0] === square) && partial.length === 1)) {
      if (partial[0] === square) partial = [];
      else if (game.moves.some((path) => path[0] === square)) partial = [square];
      render();
      return;
    }
    const options = candidates().filter((path) => path[partial.length] === square);
    if (!options.length) return;
    partial = partial.concat(square);
    const complete = options.find((path) => path.length === partial.length);
    if (complete && !options.some((path) => path.length > partial.length)) {
      const path = partial;
      game.moves = [];
      busy = true;
      render();
      send(root.dataset.moveUrl, { path, version: game.version });
      return;
    }
    render();
  });

  resignBtn.addEventListener("click", () => {
    if (window.confirm("Resign this game?" + (game.bet ? ` You'll lose your ${game.bet.toLocaleString()}-point bet.` : ""))) {
      send(root.dataset.resignUrl, {});
    }
  });

  async function refresh(force) {
    if (busy && !force) return;
    try {
      const response = await fetch(root.dataset.stateUrl, { headers: { Accept: "application/json" } });
      if (response.ok) {
        const data = await response.json();
        if (force || data.version !== game.version || data.status !== game.status) update(data);
      }
    } catch (err) { /* try again on the next tick */ }
  }

  render();
  const poll = Number(root.dataset.poll) || 1500;
  setInterval(() => {
    if (game.status === "finished" || game.status === "cancelled" || document.hidden) return;
    refresh(false);
  }, poll);
})();
