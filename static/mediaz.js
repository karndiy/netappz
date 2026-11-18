// Mediaz: a Winamp-style player for the songs and videos in Files and on this device.
(() => {
  const media = document.getElementById("mediaz-media");
  const list = document.getElementById("mediaz-tracks");
  const title = document.getElementById("mediaz-title");
  const timeText = document.getElementById("mediaz-time");
  const stateText = document.getElementById("mediaz-state");
  const seek = document.getElementById("mediaz-seek");
  const volume = document.getElementById("mediaz-volume");
  const filter = document.getElementById("mediaz-filter");
  const count = document.getElementById("mediaz-count");
  const empty = document.getElementById("mediaz-empty");
  const canvas = document.getElementById("mediaz-visual");
  const player = document.querySelector(".mediaz-player");
  if (!media || !list) return;

  // Volume, shuffle and repeat are remembered on this device.
  const saved = JSON.parse(localStorage.getItem("mediaz") || "{}");
  const options = { volume: typeof saved.volume === "number" ? saved.volume : 80, shuffle: !!saved.shuffle, repeat: !!saved.repeat };
  const save = () => localStorage.setItem("mediaz", JSON.stringify(options));
  let current = -1;
  let seeking = false;
  let failures = 0;  // Formats in a row this browser couldn't play, so a list of them doesn't go round forever.

  const tracks = () => [...list.querySelectorAll(".mediaz-track")];
  const clock = (seconds) => {
    if (!isFinite(seconds)) return "00:00";
    const whole = Math.floor(seconds);
    const hours = Math.floor(whole / 3600);
    const mm = String(Math.floor((whole % 3600) / 60)).padStart(2, "0");
    const ss = String(whole % 60).padStart(2, "0");
    return hours ? `${hours}:${mm}:${ss}` : `${mm}:${ss}`;
  };
  const setState = (text) => { stateText.textContent = text; };

  function load(index, autoplay = true) {
    const all = tracks();
    if (!all.length) return;
    current = (index + all.length) % all.length;
    const track = all[current];
    all.forEach((item) => item.removeAttribute("aria-current"));
    track.setAttribute("aria-current", "true");
    track.scrollIntoView({ block: "nearest" });
    player.classList.toggle("is-video", track.dataset.kind === "video");
    title.textContent = `${current + 1}. ${track.querySelector(".mediaz-track-name").textContent}`;
    document.title = `${track.querySelector(".mediaz-track-name").textContent} · Mediaz`;
    media.src = track.dataset.url;
    if (autoplay) play();
  }

  function play() {
    if (current < 0) return load(options.shuffle ? randomIndex() : 0);
    if (!tracks().length) return;
    startVisual();
    media.play().catch(() => {});
  }

  function stop() {
    media.pause();
    if (current >= 0) media.currentTime = 0;
    setState("■ Stopped");
  }

  function randomIndex() {
    const total = tracks().length;
    if (total < 2) return 0;
    let pick = current;
    while (pick === current) pick = Math.floor(Math.random() * total);
    return pick;
  }

  function next(step = 1) {
    if (!tracks().length) return;
    load(options.shuffle && step > 0 ? randomIndex() : current + step);
  }

  // The buttons, and the keys Winamp used: Z previous, X play, C pause, V stop, B next.
  const actions = {
    prev: () => (media.currentTime > 3 ? (media.currentTime = 0) : next(-1)),
    play: () => {
      if (current >= 0 && !media.paused) media.currentTime = 0;  // Play while playing starts it again.
      else play();
    },
    pause: () => (media.paused ? media.play().catch(() => {}) : media.pause()),
    stop,
    next: () => next(1),
    fullscreen: () => (media.requestFullscreen || media.webkitEnterFullscreen || (() => {})).call(media),
  };
  document.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", () => actions[button.dataset.action]());
  });
  document.addEventListener("keydown", (event) => {
    if (event.target.matches("input, textarea") || event.ctrlKey || event.metaKey || event.altKey) return;
    const key = { z: "prev", x: "play", c: "pause", v: "stop", b: "next" }[event.key.toLowerCase()];
    if (key) { event.preventDefault(); actions[key](); }
    else if (event.key.toLowerCase() === "s") toggles.shuffle.click();
    else if (event.key.toLowerCase() === "r") toggles.repeat.click();
    else if (event.key === " ") { event.preventDefault(); actions.pause(); }
  });

  const toggles = {};
  document.querySelectorAll("[data-toggle]").forEach((button) => {
    const name = button.dataset.toggle;
    toggles[name] = button;
    button.setAttribute("aria-pressed", String(options[name]));
    button.addEventListener("click", () => {
      options[name] = !options[name];
      button.setAttribute("aria-pressed", String(options[name]));
      save();
    });
  });

  volume.value = options.volume;
  media.volume = options.volume / 100;
  volume.addEventListener("input", () => {
    options.volume = Number(volume.value);
    media.volume = options.volume / 100;
    save();
  });

  list.addEventListener("click", (event) => {
    const track = event.target.closest(".mediaz-track");
    if (track) load(tracks().indexOf(track));
  });

  media.addEventListener("play", () => setState("▶ Playing"));
  media.addEventListener("playing", () => { failures = 0; });
  media.addEventListener("pause", () => { if (media.currentTime > 0) setState("❚❚ Paused"); });
  media.addEventListener("timeupdate", () => {
    timeText.textContent = clock(media.currentTime);
    if (!seeking && media.duration) seek.value = Math.round((media.currentTime / media.duration) * 1000);
  });
  media.addEventListener("ended", () => {
    if (options.shuffle && tracks().length > 1) return next(1);
    if (current < tracks().length - 1) return next(1);
    if (options.repeat) return load(0);
    setState("■ Stopped");
  });
  media.addEventListener("error", () => {
    if (current < 0 || !media.getAttribute("src")) return;
    setState("⚠ Can't play this format here");
    // Move on, as Winamp does, unless this was the only one.
    failures += 1;
    if (tracks().length > 1 && failures < tracks().length) setTimeout(() => next(1), 1500);
  });

  seek.addEventListener("input", () => { seeking = true; });
  seek.addEventListener("change", () => {
    if (media.duration) media.currentTime = (seek.value / 1000) * media.duration;
    seeking = false;
  });

  // Search narrows the playlist as you type.
  filter.addEventListener("input", () => {
    const words = filter.value.trim().toLowerCase();
    tracks().forEach((track) => {
      track.parentElement.hidden = words && !track.textContent.toLowerCase().includes(words);
    });
  });

  // Files opened from this device play from the browser and are never uploaded.
  document.getElementById("mediaz-files").addEventListener("change", (event) => {
    const added = [...event.target.files];
    const first = tracks().length;
    added.forEach((file) => {
      const item = document.createElement("li");
      const button = document.createElement("button");
      const kind = file.type.startsWith("video") || /\.(mp4|m4v|mov|webm|ogv|mkv|avi|wmv|3gp|mpe?g|ts|flv)$/i.test(file.name) ? "video" : "audio";
      button.type = "button";
      button.className = "mediaz-track";
      button.dataset.url = URL.createObjectURL(file);
      button.dataset.kind = kind;
      const parts = [[kind === "video" ? "🎬" : "🎵", "mediaz-track-icon"],
        [file.name.replace(/\.[^.]+$/, ""), "mediaz-track-name"], ["This device", "mediaz-track-folder"]];
      parts.forEach(([text, name]) => {
        const span = document.createElement("span");
        span.className = name;
        span.textContent = text;
        button.append(span);
      });
      item.append(button);
      list.append(item);
    });
    count.textContent = tracks().length;
    empty.hidden = tracks().length > 0;
    if (added.length && (current < 0 || media.paused)) load(first);
    event.target.value = "";
  });

  // The spectrum bars: started on the first press of Play, since browsers only allow sound after a tap.
  let analyser = null;
  function startVisual() {
    if (analyser || !window.AudioContext || !canvas) return;
    try {
      const context = new AudioContext();
      analyser = context.createAnalyser();
      analyser.fftSize = 64;
      context.createMediaElementSource(media).connect(analyser);
      analyser.connect(context.destination);
    } catch (error) {
      analyser = null;
      return;
    }
    const bars = new Uint8Array(analyser.frequencyBinCount);
    const draw = canvas.getContext("2d");
    const paint = () => {
      requestAnimationFrame(paint);
      analyser.getByteFrequencyData(bars);
      draw.clearRect(0, 0, canvas.width, canvas.height);
      const width = canvas.width / bars.length;
      bars.forEach((value, index) => {
        const height = (value / 255) * canvas.height;
        const gradient = draw.createLinearGradient(0, canvas.height, 0, 0);
        gradient.addColorStop(0, "#2fdc4b");
        gradient.addColorStop(0.7, "#e4d42a");
        gradient.addColorStop(1, "#ef3b2c");
        draw.fillStyle = gradient;
        draw.fillRect(index * width + 1, canvas.height - height, width - 2, height);
      });
    };
    paint();
  }
})();
