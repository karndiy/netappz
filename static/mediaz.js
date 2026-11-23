// Mediaz: a Winamp-style player for the songs and videos in Files, on this device and on YouTube.
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
  const isYouTube = () => current >= 0 && tracks()[current]?.dataset.kind === "youtube";

  // Volume, shuffle and repeat are remembered on this device.
  const saved = JSON.parse(localStorage.getItem("mediaz") || "{}");
  const options = { volume: typeof saved.volume === "number" ? saved.volume : 80, shuffle: !!saved.shuffle, repeat: !!saved.repeat };
  const save = () => localStorage.setItem("mediaz", JSON.stringify(options));
  let current = -1;
  let seeking = false;
  let failures = 0;  // Formats in a row this browser couldn't play, so a list of them doesn't go round forever.

  // YouTube videos play in YouTube's embedded player. Its script loads the first time one is played,
  // and these wrap it so the buttons, keys and position bar work the same for both players.
  let youtubeApi = null;
  let youtubePromise = null;
  let youtube = null;
  let youtubeTimer = null;
  function loadYouTubeApi() {
    if (!youtubeApi) {
      youtubeApi = new Promise((resolve, reject) => {
        if (window.YT && window.YT.Player) return resolve(window.YT);
        const previous = window.onYouTubeIframeAPIReady;
        window.onYouTubeIframeAPIReady = () => { if (previous) previous(); resolve(window.YT); };
        const script = document.createElement("script");
        script.src = "https://www.youtube.com/iframe_api";
        script.onerror = () => { youtubeApi = null; reject(new Error("YouTube didn't load")); };
        document.head.append(script);
      });
    }
    return youtubeApi;
  }
  function youtubePlayer() {
    if (!youtubePromise) {
      youtubePromise = loadYouTubeApi().then((YT) => new Promise((resolve) => {
        const made = new YT.Player("mediaz-youtube", {
          width: "100%",
          height: "100%",
          playerVars: { playsinline: 1, rel: 0, origin: location.origin },
          events: {
            onReady: () => { youtube = made; made.setVolume(options.volume); resolve(made); },
            onStateChange: (event) => {
              if (!isYouTube()) return;
              if (event.data === YT.PlayerState.PLAYING) { failures = 0; setState("▶ Playing"); }
              else if (event.data === YT.PlayerState.PAUSED) setState("❚❚ Paused");
              else if (event.data === YT.PlayerState.ENDED) ended();
            },
            onError: () => { if (isYouTube()) failed("⚠ This video can't play here"); },
          },
        });
      }), (error) => { youtubePromise = null; throw error; });
    }
    return youtubePromise;
  }
  // The same few controls for whichever player has the current track.
  const engine = {
    paused: () => (isYouTube() ? !youtube || youtube.getPlayerState() !== 1 : media.paused),
    time: () => (isYouTube() ? (youtube ? youtube.getCurrentTime() : 0) : media.currentTime),
    duration: () => (isYouTube() ? (youtube ? youtube.getDuration() : 0) : media.duration),
    seek: (seconds) => {
      if (isYouTube()) { if (youtube) youtube.seekTo(seconds, true); } else media.currentTime = seconds;
    },
    play: () => { if (isYouTube()) { if (youtube) youtube.playVideo(); } else media.play().catch(() => {}); },
    pause: () => { if (isYouTube()) { if (youtube) youtube.pauseVideo(); } else media.pause(); },
  };

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
    player.classList.toggle("is-youtube", track.dataset.kind === "youtube");
    title.textContent = `${current + 1}. ${track.querySelector(".mediaz-track-name").textContent}`;
    document.title = `${track.querySelector(".mediaz-track-name").textContent} · Mediaz`;
    if (track.dataset.kind === "youtube") {
      media.pause();
      media.removeAttribute("src");
      media.load();
      timeText.textContent = "00:00";
      seek.value = 0;
      setState("… Loading");
      const loading = current;
      youtubePlayer().then((made) => {
        if (current !== loading) return;  // Another track was chosen meanwhile.
        if (autoplay) made.loadVideoById(track.dataset.youtube);
        else made.cueVideoById(track.dataset.youtube);
      }).catch(() => { if (current === loading) failed("⚠ Couldn't reach YouTube"); });
      if (!youtubeTimer) youtubeTimer = setInterval(showYouTubeTime, 500);
      return;
    }
    if (youtube) youtube.stopVideo();
    media.src = track.dataset.url;
    if (autoplay) play();
  }

  function showYouTubeTime() {
    if (!isYouTube() || !youtube) return;
    const now = engine.time();
    const length = engine.duration();
    timeText.textContent = clock(now);
    if (!seeking && length) seek.value = Math.round((now / length) * 1000);
  }

  function play() {
    if (current < 0) return load(options.shuffle ? randomIndex() : 0);
    if (!tracks().length) return;
    if (!isYouTube()) startVisual();
    engine.play();
  }

  function stop() {
    if (isYouTube()) {
      if (youtube) { youtube.pauseVideo(); youtube.seekTo(0, true); }
      timeText.textContent = "00:00";
      seek.value = 0;
    } else {
      media.pause();
      if (current >= 0) media.currentTime = 0;
    }
    setState("■ Stopped");
  }

  function ended() {
    if (options.shuffle && tracks().length > 1) return next(1);
    if (current < tracks().length - 1) return next(1);
    if (options.repeat) return load(0);
    setState("■ Stopped");
  }

  function failed(message) {
    setState(message);
    // Move on, as Winamp does, unless this was the only one.
    failures += 1;
    if (tracks().length > 1 && failures < tracks().length) setTimeout(() => next(1), 1500);
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
    prev: () => (engine.time() > 3 ? engine.seek(0) : next(-1)),
    play: () => {
      if (current >= 0 && !engine.paused()) engine.seek(0);  // Play while playing starts it again.
      else play();
    },
    pause: () => (engine.paused() ? engine.play() : engine.pause()),
    stop,
    next: () => next(1),
    fullscreen: () => {
      const target = isYouTube() && youtube ? youtube.getIframe() : media;
      (target.requestFullscreen || target.webkitEnterFullscreen || (() => {})).call(target);
    },
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
    if (youtube) youtube.setVolume(options.volume);
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
  media.addEventListener("ended", ended);
  media.addEventListener("error", () => {
    if (current < 0 || isYouTube() || !media.getAttribute("src")) return;
    failed("⚠ Can't play this format here");
  });

  seek.addEventListener("input", () => { seeking = true; });
  seek.addEventListener("change", () => {
    const length = engine.duration();
    if (length) engine.seek((seek.value / 1000) * length);
    seeking = false;
  });

  // Search narrows the playlist as you type.
  filter.addEventListener("input", () => {
    const words = filter.value.trim().toLowerCase();
    tracks().forEach((track) => {
      track.parentElement.hidden = words && !track.textContent.toLowerCase().includes(words);
    });
  });

  // Adds one track to the end of the playlist and returns its button.
  function addTrack({ kind, name, folder, url = "", youtubeId = "" }) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "mediaz-track";
    button.dataset.url = url;
    button.dataset.kind = kind;
    if (youtubeId) button.dataset.youtube = youtubeId;
    const icon = { video: "🎬", youtube: "▶️" }[kind] || "🎵";
    [[icon, "mediaz-track-icon"], [name, "mediaz-track-name"], [folder, "mediaz-track-folder"]].forEach(([text, style]) => {
      const span = document.createElement("span");
      span.className = style;
      span.textContent = text;
      button.append(span);
    });
    item.append(button);
    list.append(item);
    count.textContent = tracks().length;
    empty.hidden = true;
    return button;
  }

  // Files opened from this device play from the browser and are never uploaded.
  document.getElementById("mediaz-files").addEventListener("change", (event) => {
    const added = [...event.target.files];
    const first = tracks().length;
    added.forEach((file) => {
      const kind = file.type.startsWith("video") || /\.(mp4|m4v|mov|webm|ogv|mkv|avi|wmv|3gp|mpe?g|ts|flv)$/i.test(file.name) ? "video" : "audio";
      addTrack({ kind, name: file.name.replace(/\.[^.]+$/, ""), folder: "This device", url: URL.createObjectURL(file) });
    });
    if (added.length && (current < 0 || engine.paused())) load(first);
    event.target.value = "";
  });

  // YouTube search: the server looks the words up (or reads a pasted link) and sends back videos.
  const ytForm = document.getElementById("mediaz-yt-form");
  const ytQuery = document.getElementById("mediaz-yt-query");
  const ytStatus = document.getElementById("mediaz-yt-status");
  const ytResults = document.getElementById("mediaz-yt-results");
  const ytFound = new Map();
  const ytStatusText = (text) => { ytStatus.textContent = text; ytStatus.hidden = !text; };
  function addYouTube(video) {
    const already = tracks().find((track) => track.dataset.youtube === video.id);
    return already || addTrack({ kind: "youtube", name: video.title, folder: ["YouTube", video.channel, video.duration].filter(Boolean).join(" · "), youtubeId: video.id });
  }
  if (ytForm) {
    ytForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const words = ytQuery.value.trim();
      if (!words) return;
      ytStatusText("Searching YouTube…");
      ytResults.replaceChildren();
      ytFound.clear();
      try {
        const response = await fetch(`${ytForm.action}?q=${encodeURIComponent(words)}`, { headers: { Accept: "application/json" } });
        const answer = await response.json().catch(() => ({}));
        if (!response.ok) return ytStatusText(answer.error || "The search didn't work. Try again.");
        const videos = answer.videos || [];
        ytStatusText(videos.length ? "" : "No videos found. Try other words.");
        videos.forEach((video) => {
          ytFound.set(video.id, video);
          const item = document.createElement("li");
          const open = document.createElement("button");
          open.type = "button";
          open.className = "mediaz-yt-result";
          open.dataset.play = video.id;
          const picture = document.createElement("img");
          picture.src = video.thumbnail;
          picture.alt = "";
          picture.loading = "lazy";
          const text = document.createElement("span");
          const name = document.createElement("strong");
          name.textContent = video.title;
          const about = document.createElement("small");
          about.textContent = [video.channel, video.duration].filter(Boolean).join(" · ");
          text.append(name, about);
          open.append(picture, text);
          const add = document.createElement("button");
          add.type = "button";
          add.className = "secondary mediaz-yt-add";
          add.dataset.add = video.id;
          add.title = "Add to the playlist";
          add.setAttribute("aria-label", `Add ${video.title} to the playlist`);
          add.textContent = "＋";
          item.append(open, add);
          ytResults.append(item);
        });
      } catch (error) {
        ytStatusText("Couldn't reach the server. Check your connection and try again.");
      }
    });
    ytResults.addEventListener("click", (event) => {
      const button = event.target.closest("button");
      const video = button && ytFound.get(button.dataset.play || button.dataset.add);
      if (!video) return;
      const track = addYouTube(video);
      if (button.dataset.play) load(tracks().indexOf(track));
      else { button.textContent = "✓"; button.disabled = true; }
    });
  }

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
