// Reveal in Finder and Copy path buttons on a file's page.
(function () {
  const status = document.querySelector(".status");
  const say = (msg) => { if (status) status.textContent = msg; };

  document.querySelectorAll("button.reveal").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const body = new URLSearchParams({ id: btn.dataset.id });
      try {
        const r = await fetch("/reveal", { method: "POST", body });
        const j = await r.json();
        say(j.message);
      } catch (e) {
        say("Couldn't reach the archive app. Is it still running?");
      }
    });
  });

  document.querySelectorAll("button.delete").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!window.confirm(`Delete ${btn.dataset.what} from the archive drive? This can't be undone.`)) return;
      const body = new URLSearchParams(btn.dataset.uid ? { uid: btn.dataset.uid } : { id: btn.dataset.id });
      btn.disabled = true;
      say("Deleting…");
      try {
        const r = await fetch("/delete", { method: "POST", body });
        const j = await r.json();
        say(j.message);
        if (j.ok) setTimeout(() => { j.gone ? (window.location = "/search") : window.location.reload(); }, 900);
        else btn.disabled = false;
      } catch (e) {
        say("Couldn't reach the archive app. Is it still running?");
        btn.disabled = false;
      }
    });
  });

  document.querySelectorAll("button.copypath").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(btn.dataset.path);
        say("Path copied.");
      } catch (e) {
        say(btn.dataset.path);
      }
    });
  });
})();
