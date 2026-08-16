/* ============================================================================
 * estrai_forexfactory.js  —  v1.0
 *
 * COSA FA
 *   Legge gli eventi del calendario GIA' CARICATI nella pagina Forex Factory
 *   che hai aperto tu, e li salva in un file .json sul tuo computer.
 *   ZERO richieste di rete. Nessun login aggirato. Nessun crawling.
 *
 * COME SI USA
 *   1. Apri  https://www.forexfactory.com/calendar?range=aug1.2019-oct2.2019
 *      (cambia il range come vuoi: gen1.2020-dec31.2020, ecc.)
 *   2. F12 -> Console.  In Chrome la prima volta: scrivi  allow pasting  + Invio.
 *   3. Incolla TUTTO questo file, Invio.
 *   4. Parte il download del .json.
 *   5. Carica il file nella pagina  /storico  del Macro Scenario Engine
 *      (oppure POST /api/v1/admin/calendar/import-forexfactory-file).
 *
 *   NON serve scorrere fino in fondo: i dati dell'intero range sono gia'
 *   presenti nello stato della pagina al primo caricamento (verificato).
 *
 * FONTE DEI DATI
 *   window.calendarComponentStates[N].days[].events[]
 *   Il campo `dateline` e' un timestamp UNIX ASSOLUTO (UTC): nessuna ambiguita'
 *   di fuso orario, qualunque sia il timezone impostato sul sito.
 *
 * SE LA STRUTTURA DEL SITO CAMBIA
 *   Lo script si ferma con un errore esplicito. Non produce un file vuoto
 *   fingendo di aver funzionato.
 *
 * SE LA CONSOLE NON E' UN'OPZIONE
 *   Salva la pagina con Ctrl+S e carica direttamente il file .html: contiene
 *   uno <script> con lo stesso stato e gli stessi timestamp assoluti, quindi la
 *   fedelta' e' identica. Non serve impostare il fuso su GMT.
 *   In Chrome scegli "Pagina web, completa" o "Solo HTML": "Singolo file"
 *   produce un archivio MHTML, che non contiene lo stato.
 * ========================================================================== */

(function () {
  'use strict';

  var SCHEMA = 'ff-calendar/1';

  function fail(msg) {
    console.error('%c[estrai_forexfactory] ERRORE: ' + msg,
      'color:#fff;background:#c0392b;padding:2px 6px;border-radius:3px');
    throw new Error(msg);
  }

  // ---- 1. Localizza lo stato del calendario --------------------------------
  var states = window.calendarComponentStates;
  if (!states || typeof states !== 'object') {
    fail('window.calendarComponentStates non trovato. ' +
         'Sei sicuro di essere su forexfactory.com/calendar e che la pagina ' +
         'sia caricata del tutto? Struttura del sito forse cambiata.');
  }

  var state = null;
  for (var k in states) {
    if (states[k] && Array.isArray(states[k].days)) { state = states[k]; break; }
  }
  if (!state) fail('Nessuno stato con array `days`. Struttura del sito cambiata.');
  if (!state.days.length) fail('Array `days` vuoto: il range selezionato non contiene giorni.');

  // ---- 2. Estrai gli eventi ------------------------------------------------
  var events = [];
  var skipped = 0;

  state.days.forEach(function (day) {
    var list = day.events || [];
    list.forEach(function (e) {
      if (!e || typeof e.dateline !== 'number') { skipped++; return; }

      events.push({
        id:       e.id,                    // id evento FF (stabile -> usalo per il dedup)
        ebaseId:  e.ebaseId,               // id della "serie" (es. NFP = sempre stesso ebaseId)
        ts:       e.dateline,              // UNIX secondi, UTC ASSOLUTO
        utc:      new Date(e.dateline * 1000).toISOString(),
        currency: e.currency || null,
        country:  e.country  || null,
        title:    e.name || e.prefixedName || null,
        impact:   e.impactName || null,    // low | medium | high | holiday
        timeLabel:  e.timeLabel || null,   // etichetta come mostrata dal sito
        timeApprox: !!e.timeMasked,        // true => "All Day"/"Tentative": ORARIO NON AFFIDABILE
        actual:   e.actual   === '' ? null : e.actual,
        forecast: e.forecast === '' ? null : e.forecast,
        previous: e.previous === '' ? null : e.previous,
        revision: e.revision === '' ? null : e.revision,
        actualBetterWorse:   e.actualBetterWorse,    // >0 meglio, <0 peggio, 0 in linea
        revisionBetterWorse: e.revisionBetterWorse
      });
    });
  });

  if (!events.length) {
    fail('Zero eventi estratti da ' + state.days.length + ' giorni. ' +
         'O il range e\' vuoto, o i filtri del sito escludono tutto, ' +
         'o la struttura e\' cambiata. Nessun file prodotto.');
  }

  // ---- 3. Metadati ---------------------------------------------------------
  events.sort(function (a, b) { return a.ts - b.ts || a.id - b.id; });

  var iso = function (ts) { return new Date(ts * 1000).toISOString().slice(0, 10); };
  var payload = {
    schema:      SCHEMA,
    source:      'forexfactory.com/calendar',
    sourceUrl:   location.href,
    extractedAt: new Date().toISOString(),
    method:      'dom-state',              // timestamp UNIX assoluti, non tabella HTML
    days:        state.days.length,
    count:       events.length,
    skipped:     skipped,
    range:       { from: iso(events[0].ts), to: iso(events[events.length - 1].ts) },
    events:      events
  };

  // ---- 4. Download ---------------------------------------------------------
  var fname = 'ff_' + payload.range.from + '_' + payload.range.to + '.json';
  var blob  = new Blob([JSON.stringify(payload)], { type: 'application/json' });
  var url   = URL.createObjectURL(blob);
  var a     = document.createElement('a');
  a.href = url; a.download = fname;
  document.body.appendChild(a); a.click();
  setTimeout(function () { URL.revokeObjectURL(url); a.remove(); }, 1000);

  console.log('%c[estrai_forexfactory] OK',
    'color:#fff;background:#27ae60;padding:2px 6px;border-radius:3px',
    '\n  file      : ' + fname +
    '\n  eventi    : ' + events.length +
    '\n  giorni    : ' + state.days.length +
    '\n  range     : ' + payload.range.from + ' -> ' + payload.range.to +
    '\n  scartati  : ' + skipped);

  return payload;
})();
