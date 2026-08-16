/* Macro Scenario Engine — countdown, orologio e auto-refresh della dashboard. */
(function () {
  "use strict";

  // Scarto fra l'orologio del browser e quello del server (ms).
  var skew = 0;
  var clockEl = document.getElementById("clock");
  if (clockEl && clockEl.dataset.utc) {
    var serverNow = Date.parse(clockEl.dataset.utc + "Z");
    if (!isNaN(serverNow)) {
      skew = serverNow - Date.now();
    }
  }

  function nowUtc() {
    return new Date(Date.now() + skew);
  }

  function two(n) {
    return (n < 10 ? "0" : "") + n;
  }

  function tickClock() {
    if (!clockEl) return;
    var d = nowUtc();
    clockEl.textContent =
      two(d.getUTCHours()) + ":" + two(d.getUTCMinutes()) + ":" + two(d.getUTCSeconds());
  }

  function formatDelta(ms) {
    var past = ms < 0;
    var total = Math.floor(Math.abs(ms) / 1000);
    var days = Math.floor(total / 86400);
    var hours = Math.floor((total % 86400) / 3600);
    var minutes = Math.floor((total % 3600) / 60);
    var seconds = total % 60;
    var text;
    if (days > 0) {
      text = days + "g " + two(hours) + "h" + two(minutes);
    } else if (hours > 0) {
      text = two(hours) + ":" + two(minutes) + ":" + two(seconds);
    } else {
      text = two(minutes) + ":" + two(seconds);
    }
    return past ? "-" + text : text;
  }

  function tickCountdowns() {
    var nodes = document.querySelectorAll(".countdown[data-ts]");
    var current = nowUtc().getTime();
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      var target = Date.parse(node.dataset.ts);
      if (isNaN(target)) continue;
      var delta = target - current;
      node.textContent = formatDelta(delta);
      node.classList.remove("urgent", "soon");
      if (delta > 0 && delta <= 30 * 60 * 1000) {
        node.classList.add("urgent");
      } else if (delta > 0 && delta <= 4 * 60 * 60 * 1000) {
        node.classList.add("soon");
      }
    }
  }

  tickClock();
  tickCountdowns();
  setInterval(tickClock, 1000);
  setInterval(tickCountdowns, 1000);

  // Pulsante "Scarica i dati adesso": lancia i collector sul server.
  var bottoni = document.querySelectorAll('[data-azione="collect"]');
  for (var b = 0; b < bottoni.length; b++) {
    bottoni[b].addEventListener("click", function (evento) {
      var bottone = evento.currentTarget;
      var testoIniziale = bottone.textContent;
      bottone.disabled = true;
      bottone.textContent = "Scaricamento in corso...";
      fetch("/api/v1/admin/collect", { method: "POST" })
        .then(function (risposta) {
          if (!risposta.ok) throw new Error("HTTP " + risposta.status);
          return risposta.json();
        })
        .then(function () {
          bottone.textContent = "Fatto, ricarico...";
          window.location.reload();
        })
        .catch(function (errore) {
          bottone.disabled = false;
          bottone.textContent = testoIniziale;
          alert(
            "Scaricamento non riuscito: " + errore.message +
            "\n\nControlla la connessione a internet e la pagina Fonti " +
            "per lo stato di ogni singola fonte."
          );
        });
    });
  }

  // --- Pagina "Storico": import file e rigiocata ---------------------------
  // Ogni operazione mostra l'esito completo restituito dal server, comprese le
  // avvertenze: un import silenzioso e' un import di cui non ci si puo' fidare.

  function mostraEsito(contenitore, dati, errore) {
    if (!contenitore) return;
    if (errore) {
      contenitore.innerHTML =
        '<div class="notice bad"><strong>Operazione non riuscita.</strong> ' +
        String(errore).replace(/</g, "&lt;") +
        "</div>";
      return;
    }
    var righe = [];
    if (dati.eventi !== undefined) {
      righe.push(
        "Eventi riconosciuti: <strong>" + dati.eventi + "</strong> (nuovi " +
        (dati.creati || 0) + ", aggiornati " + (dati.aggiornati || 0) +
        ", sorprese calcolate " + (dati.sorprese_calcolate || 0) + ")."
      );
      if (dati.periodo) {
        righe.push("Periodo coperto: " + dati.periodo[0].slice(0, 10) + " → " + dati.periodo[1].slice(0, 10) + ".");
      }
      righe.push("Metodo di lettura: <code>" + (dati.metodo || "n/d") + "</code>.");
    }
    if (dati.prezzi_inseriti !== undefined) {
      righe.push("Barre di prezzo inserite: <strong>" + dati.prezzi_inseriti + "</strong>.");
    }
    if (dati.scenari_generati !== undefined) {
      righe.push(
        "Scenari generati: <strong>" + dati.scenari_generati + "</strong> su " +
        dati.passi + " date" + (dati.passi_falliti ? " (" + dati.passi_falliti + " date fallite)" : "") + "."
      );
      var tassi = (dati.tassi_ricostruiti || []).join(", ");
      righe.push("Tassi di policy ricostruiti per: " + (tassi || "nessuna valuta"));
    }
    if (dati.valutazione) {
      righe.push(
        "Valutati contro i prezzi: <strong>" + (dati.valutazione.valutati || 0) +
        "</strong> su " + (dati.valutazione.candidati || 0) +
        " scaduti; senza prezzi: " + (dati.valutazione.senza_prezzi || 0) + "."
      );
    }
    if (dati.copertura) {
      var cop = dati.copertura;
      var imp = Object.keys(cop.per_impatto || {}).map(function (k) {
        return k + " " + cop.per_impatto[k];
      }).join(" · ");
      var anni = Object.keys(cop.per_anno || {});
      righe.push(
        "Copertura: " + anni.length + " anni (" + anni[0] + "–" + anni[anni.length - 1] +
        "), " + Object.keys(cop.per_valuta || {}).length + " valute. Impatto: " + imp + "."
      );
      righe.push(
        "Con actual: " + cop.con_actual + " · con consenso: " + cop.con_forecast +
        " · con revisione: " + cop.con_revisione + "."
      );
    }
    (dati.avvisi || []).forEach(function (a) { righe.push(a); });
    if (dati.avviso) righe.push(dati.avviso);

    var classe = (dati.eventi === 0 || dati.scenari_generati === 0) ? "warn" : "ok";
    contenitore.innerHTML =
      '<div class="notice ' + classe + '"><ul class="tight"><li>' +
      righe.join("</li><li>") + "</li></ul></div>";
  }

  function inviaForm(form, url, esitoId, corpoJson) {
    if (!form) return;
    form.addEventListener("submit", function (evento) {
      evento.preventDefault();
      var bottone = form.querySelector("button[type=submit]");
      var etichetta = bottone ? bottone.textContent : "";
      var esito = document.getElementById(esitoId);
      if (bottone) {
        bottone.disabled = true;
        bottone.textContent = "In corso...";
      }
      if (esito) esito.innerHTML = '<p class="muted small">Elaborazione in corso, non chiudere la pagina.</p>';

      var opzioni;
      if (corpoJson) {
        opzioni = {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(corpoJson(form))
        };
      } else {
        opzioni = { method: "POST", body: new FormData(form) };
      }

      fetch(url(form), opzioni)
        .then(function (risposta) {
          // Il corpo puo' non essere JSON (es. "Internal Server Error" in
          // testo semplice): si legge come testo e si prova a interpretarlo,
          // cosi' l'errore vero arriva all'utente invece di "not valid JSON".
          return risposta.text().then(function (testo) {
            var dati;
            try { dati = JSON.parse(testo); } catch (e) { dati = null; }
            if (!risposta.ok) {
              var motivo = (dati && dati.detail) || testo.slice(0, 300) || ("HTTP " + risposta.status);
              throw new Error(motivo);
            }
            return dati || {};
          });
        })
        .then(function (dati) { mostraEsito(esito, dati, null); })
        .catch(function (err) { mostraEsito(esito, {}, err.message); })
        .then(function () {
          if (bottone) {
            bottone.disabled = false;
            bottone.textContent = etichetta;
          }
        });
    });
  }

  inviaForm(
    document.getElementById("form-mt5"),
    function (form) {
      var offset = ((form.querySelector("[name=offset]") || {}).value || "").trim().replace(",", ".");
      var url = "/api/v1/admin/calendar/import-mt5-file";
      if (offset !== "" && !isNaN(parseFloat(offset))) {
        url += "?gmt_offset_hours=" + encodeURIComponent(offset);
      }
      return url;
    },
    "esito-mt5"
  );

  inviaForm(
    document.getElementById("form-storico"),
    function () { return "/api/v1/admin/calendar/import-forexfactory-file"; },
    "esito-storico"
  );

  inviaForm(
    document.getElementById("form-prezzi"),
    function (form) {
      var coppia = (form.querySelector("[name=pair]") || {}).value || "";
      return "/api/v1/admin/prices/import-file?pair=" + encodeURIComponent(coppia.trim());
    },
    "esito-prezzi"
  );

  inviaForm(
    document.getElementById("form-replay"),
    function () { return "/api/v1/admin/replay"; },
    "esito-replay",
    function (form) {
      return {
        start: form.querySelector("[name=start]").value + "T00:00:00",
        end: form.querySelector("[name=end]").value + "T23:59:59",
        step_hours: parseInt(form.querySelector("[name=step_hours]").value, 10)
      };
    }
  );

  // Ricarica periodica della pagina: la dashboard e' server-side, quindi il
  // refresh e' il modo piu' semplice e robusto per restare allineati.
  var interval = parseInt(document.body.dataset.refresh || "0", 10);
  if (interval > 0) {
    setTimeout(function () {
      window.location.reload();
    }, interval * 1000);
  }
})();
