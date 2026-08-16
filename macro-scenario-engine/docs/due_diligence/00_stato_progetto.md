# Stato del progetto — Macro Scenario Engine
*Aggiornato al 16/08/2026, commit `f5c6883`. Questo documento e' il punto di
partenza della due diligence: riassume il percorso, fotografa dove siamo,
elenca pro, contro e domande aperte. Ogni numero citato e' stato misurato
sui dati reali dell'utente, non stimato.*

---

## 1. Il percorso (gli stati attraversati dal processo)

| Stato | Cosa e' successo | Cosa abbiamo imparato |
|---|---|---|
| **Nascita** | Motore costruito da zero: calendario → catene causali → score → 3 scenari probabilistici per coppia, dashboard, bridge MT5. | L'architettura regge; senza dati storici pero' tutto restava NEUTRAL. |
| **Caccia ai dati** | Forex Factory non scaricabile; costruita l'estrazione dal browser; poi la svolta: l'exporter MQL5 del terminale MT5 (209.309 record, 2007→2026). | La fonte migliore era gia' sul PC dell'utente. Il calendario MT5 e' localizzato in italiano: i pattern di classificazione dovevano esserlo. |
| **Fatica e rifacimenti** | Import >10 minuti, errori 500, prezzi a 0 righe, tassi "nessuna valuta", troncamento silenzioso della rigiocata. Momenti di vera frustrazione ("prima che rifaccio tutto per la 20ª volta..."). | Ogni difetto aveva una causa precisa e misurabile. Da allora: nessun passo chiesto all'utente senza verifica completa in sandbox prima. |
| **Prima verita'** | Rigiocata completa: 20.901 scenari valutati. Verdetto duro: hit 19,7%, LATERALE previsto 97,6% contro ~20% reale, confidenza invertita. | Il modello non era cieco: era mal tarato in modo preciso. Le (poche) chiamate direzionali a soglia 15 centravano il 37-42%. |
| **Ricalibrazione (CICLO 4)** | Banda neutra adattiva (vol×orizzonte, esiti equiprobabili sotto il nullo), soglia direzione 8, compressione probabilita' λ=0.5, LATERALE senza segnale mai sopra BASSA. | Rimisurato su 35.379 scenari: **calibrazione risolta** (scarto +1,1 contro −24,2), monocultura laterale rientrata (97,6%→75,1%). |
| **Secondo verdetto (CICLO 5)** | Ma: hit direzionale a soglia 8 = 26-29% ≈ frequenza di base (margine diluito); scala di confidenza illeggibile per composizione. Costruito lo strumento di misura: `per_bias` e `per_confidenza` divisa per classe. | Nessun parametro va piu' toccato a occhio: prima si misura, poi si decide. |

## 2. Dove siamo ora (fotografia)

- **Dati**: 202.192 eventi di calendario (2007→2026, 8 valute, italiano),
  42.636 barre D1 su 7 coppie, archivio da ~324 MB sul PC dell'utente.
- **Pipeline**: import → classificazione → sorprese → score → scenari →
  rigiocata point-in-time (orologio congelato, anti-lookahead testato) →
  valutazione con banda adattiva → metriche di affidabilita'.
- **Ultima misura (35.379 scenari)**: hit globale 38,5%, Brier 0,238,
  calibrazione a posto (bucket dominante 38,8% dichiarato vs 39,9% reale),
  LATERALE 75,1% previsto vs 42,1% reale, direzionali senza margine a
  soglia 8, previsto RIALZO/RIBASSO 1,55× contro un realizzato simmetrico.
- **Strumenti pronti e non ancora letti sui dati reali**: `per_bias`
  (hit per fascia di |bias|) e `per_confidenza_direzionale/laterale`.
- **Qualita'**: 170 test automatici verdi; migrazioni di schema additive;
  ogni cap/limite dichiarato nei report.

## 3. Pro (cio' che abbiamo e che pochi hanno)

1. **Un laboratorio di verita'**: 19,6 anni di dati reali + rigiocata
   anti-lookahead + metriche oneste = ogni ipotesi si puo' misurare in
   ~2 minuti per segmento, senza aspettare il mercato.
2. **Calibrazione risolta**: quando il modello dice 39%, accade il 39-40%
   delle volte. E' il prerequisito di qualunque uso operativo.
3. **Un segnale direzionale esiste**: a soglia alta (15) le chiamate
   direzionali battevano la base rate di 8-13 punti. Va ritrovata la soglia
   dove quel margine vive, con `per_bias`.
4. **Processo disciplinato**: nessuna modifica senza numero che la
   giustifichi; limiti dichiarati; test per ogni correzione; audit trail.
5. **Filiera completa**: dal terminale MT5 alla dashboard al bridge EA
   (DRY-RUN), tutto riproducibile dall'utente con doppi clic.

## 4. Contro e rischi (da guardare in faccia)

1. **Margine direzionale non dimostrato alla taratura attuale**: a soglia 8
   l'hit direzionale ≈ base rate. Finche' `per_bias` non indica una soglia
   con margine, il sistema e' onesto ma non "predittivo".
2. **Confidenza da ridefinire**: oggi ordina male (o non ordina) l'esito;
   va ricostruita su grandezze che predicono davvero l'hit.
3. **Bias rialzista strutturale** nelle previsioni direzionali (1,55×):
   possibile asimmetria nello scoring; indiziato da verificare.
4. **Lookahead residuo non eliminabile**: il calendario riporta valori
   revisionati, non la prima stampa. L'hit-rate storico e' quindi un tetto
   ottimistico, non una promessa.
5. **Nessun costo di esecuzione**: spread/slippage/commissioni non entrano;
   "direzione azzeccata" ≠ "trade profittevole".
6. **Sotto-score poveri nel passato**: cb_stance quasi sempre neutro in
   rigiocata (niente comunicati storici); il modello reale e' piu' ricco di
   quello rigiocato.
7. **Rischio overfitting**: con 35k scenari e molti parametri, si puo'
   "vincere sul passato" per costruzione. Ogni taratura va validata su un
   periodo MAI usato per sceglierla (split temporale).
8. **XAU e' un proxy inverso del dollaro**, non un modello dell'oro.

## 5. Domande aperte per la due diligence

1. In quale fascia di |bias| l'hit direzionale batte la base rate, e con
   che campione? (=> soglia di direzione misurata)
2. Il margine sopravvive a uno split temporale train/test? E per coppia?
3. Quali grandezze predicono l'hit (qualita' dati? convergenza? |bias|?
   rischio evento? orizzonte?) => nuova formula della confidenza.
4. Da dove viene il bias rialzista? (pesi, sorprese asimmetriche, USD?)
5. I pesi dei sei sotto-score sono difendibili? Nessuno e' mai stato
   validato singolarmente contro gli esiti.
6. L'orizzonte h24_72 e' quello giusto? Il margine potrebbe vivere su
   orizzonti diversi.
7. La compressione λ=0.5 e' ancora corretta dopo ogni altra modifica?
   (la calibrazione va rimisurata a valle di tutto)

## 6. Regole non negoziabili (dal mandato del progetto)

- Niente look-ahead; punto-nel-tempo sempre; niente cap silenziosi.
- Le probabilita' sono STIME DI MODELLO, mai garanzie.
- Cio' che non e' fattibile non si finge: si implementa il fallback, si
  logga il limite e si dichiara.
- Non cancellare mai dati; il database non si ricrea; solo copie.
- Nessun parametro cambiato senza il numero che lo giustifica, e nessuna
  taratura accettata senza validazione su dati mai usati per sceglierla.
