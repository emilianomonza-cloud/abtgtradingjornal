//+------------------------------------------------------------------+
//|                                   CalendarHistoryExporter.mq5     |
//|                                   Emiliano Monza                  |
//|                                                                   |
//| Export completo dello storico del calendario economico del        |
//| terminale MetaTrader 5 in CSV / JSON / JSONL + metadati + report   |
//| di copertura.                                                     |
//|                                                                   |
//| Fonte dati: database calendario del terminale MT5, esposto        |
//| dall'API pubblica documentata MQL5 (CalendarValueHistory,         |
//| CalendarEventById, CalendarCountryById). Nessuna richiesta web,    |
//| nessuno scraping, nessun accesso fuori dall'interfaccia fornita.   |
//|                                                                   |
//| CHANGELOG                                                         |
//|  1.00  Prima versione: discovery, chunking adattivo, dedup,       |
//|        export CSV/JSON/JSONL, metadati, report di copertura.      |
//+------------------------------------------------------------------+
#property copyright   "Emiliano Monza"
#property version     "1.00"
#property description "Esporta lo storico del calendario economico MT5 in CSV/JSON/JSONL con report di copertura."
#property script_show_inputs

//====================================================================
//  PARAMETRI
//====================================================================
input group "=== Intervallo ==="
input bool     InpAutoDiscoverStart = true;        // Scopri automaticamente la prima data disponibile
input datetime InpDateFrom          = D'2006.01.01 00:00';  // Data inizio (se discovery = false)
input datetime InpDateTo            = 0;           // Data fine (0 = adesso)

input group "=== Filtri (vuoto = tutto) ==="
input string   InpCountryCode       = "";          // Codice paese ISO 3166-1 alpha-2 (es. US)
input string   InpCurrency          = "";          // Valuta (es. USD)
input bool     InpOnlyWithActual    = false;       // Esporta solo eventi con valore Actual

input group "=== Acquisizione ==="
input int      InpChunkMonths       = 3;           // Ampiezza blocco iniziale in mesi
input int      InpMaxRetries        = 4;           // Tentativi per blocco prima di dimezzarlo
input int      InpPauseMs           = 20;          // Pausa fra blocchi (ms)

input group "=== Fuso orario ==="
input double   InpServerGmtOffset   = 999.0;       // Offset GMT del server in ore (999 = non dichiarato -> time_utc null)

input group "=== Output ==="
input string   InpPrefix            = "mt5_calendar";  // Prefisso dei file
input bool     InpUseCommonFolder   = false;       // Scrivi in Common\Files invece che in MQL5\Files
input bool     InpExportCSV         = true;        // Esporta CSV
input bool     InpExportJSON        = true;        // Esporta JSON
input bool     InpExportJSONL       = true;        // Esporta JSONL

//====================================================================
//  COSTANTI
//====================================================================
#define PARSER_VERSION   "1.00"
#define VAL_SCALE        1000000.0
#define ERR_CAL_MORE     5400
#define ERR_CAL_TIMEOUT  5401
#define MIN_CHUNK_DAYS   3

//====================================================================
//  STRUTTURE
//====================================================================

//--- Un record esportabile: valore + metadati di evento e paese risolti
struct CalRecord
{
   ulong    value_id;
   ulong    event_id;
   datetime time_server;
   datetime period;
   int      revision;

   long     actual_raw;
   long     forecast_raw;
   long     prev_raw;
   long     revised_prev_raw;

   string   impact_type;

   string   event_name;
   string   event_code;
   string   event_type;
   string   sector;
   string   frequency;
   string   time_mode;
   string   importance;
   string   unit;
   string   multiplier;
   int      digits;
   string   source_url;

   ulong    country_id;
   string   country_name;
   string   country_code;
   string   currency;
};

//--- Cache metadati evento (evita una CalendarEventById per ogni valore)
struct EventCache
{
   ulong             id;
   MqlCalendarEvent  ev;
   bool              ok;
};

//--- Cache metadati paese
struct CountryCache
{
   ulong                id;
   MqlCalendarCountry   ct;
   bool                 ok;
};

//--- Esito di un blocco temporale
struct ChunkStatus
{
   datetime from;
   datetime to;
   int      values;
   string   state;      // completed | empty_verified | failed_explained
   string   reason;
};

//====================================================================
//  STATO GLOBALE
//====================================================================
CalRecord     g_records[];
EventCache    g_eventCache[];
CountryCache  g_countryCache[];
ChunkStatus   g_chunks[];

int  g_dupRemoved      = 0;
int  g_eventLookupFail = 0;
int  g_rawCollected    = 0;
int  g_discoveryErrors = 0;

//====================================================================
//  SCRITTORE DI FILE UTF-8 IN STREAMING
//  MQL5 in FILE_TXT|FILE_ANSI userebbe la codepage ANSI e perderebbe
//  i caratteri non ASCII: apriamo in binario e convertiamo in UTF-8.
//====================================================================
class CUtf8Writer
{
private:
   int    m_handle;
   string m_name;
public:
   CUtf8Writer(void): m_handle(INVALID_HANDLE) {}
  ~CUtf8Writer(void) { Close(); }

   //--- Apre il file. bom=true antepone il BOM UTF-8 (richiesto da Excel).
   bool Open(const string filename, const bool bom)
   {
      m_name = filename;
      int flags = FILE_WRITE | FILE_BIN;
      if(InpUseCommonFolder) flags |= FILE_COMMON;

      m_handle = FileOpen(filename, flags);
      if(m_handle == INVALID_HANDLE)
      {
         PrintFormat("ERRORE apertura file '%s': %d", filename, GetLastError());
         return false;
      }
      if(bom)
      {
         uchar b[3] = {0xEF, 0xBB, 0xBF};
         FileWriteArray(m_handle, b, 0, 3);
      }
      return true;
   }

   //--- Accoda una stringa convertita in UTF-8.
   void Write(const string s)
   {
      if(m_handle == INVALID_HANDLE || StringLen(s) == 0) return;
      uchar buf[];
      int n = StringToCharArray(s, buf, 0, -1, CP_UTF8);
      if(n > 0) n--;                       // scarta il terminatore nullo
      if(n > 0) FileWriteArray(m_handle, buf, 0, n);
   }

   void Close(void)
   {
      if(m_handle != INVALID_HANDLE) { FileClose(m_handle); m_handle = INVALID_HANDLE; }
   }

   string Name(void) const { return m_name; }
};

//====================================================================
//  CONVERSIONI ENUM -> STRINGA
//  Il default restituisce "unknown_<n>": mai un'etichetta inventata.
//====================================================================
string ImportanceToStr(const ENUM_CALENDAR_EVENT_IMPORTANCE v)
{
   switch(v)
   {
      case CALENDAR_IMPORTANCE_NONE:     return "none";
      case CALENDAR_IMPORTANCE_LOW:      return "low";
      case CALENDAR_IMPORTANCE_MODERATE: return "moderate";
      case CALENDAR_IMPORTANCE_HIGH:     return "high";
   }
   return StringFormat("unknown_%d", (int)v);
}

string TypeToStr(const ENUM_CALENDAR_EVENT_TYPE v)
{
   switch(v)
   {
      case CALENDAR_TYPE_EVENT:     return "event";
      case CALENDAR_TYPE_INDICATOR: return "indicator";
      case CALENDAR_TYPE_HOLIDAY:   return "holiday";
   }
   return StringFormat("unknown_%d", (int)v);
}

string SectorToStr(const ENUM_CALENDAR_EVENT_SECTOR v)
{
   switch(v)
   {
      case CALENDAR_SECTOR_NONE:       return "none";
      case CALENDAR_SECTOR_MARKET:     return "market";
      case CALENDAR_SECTOR_GDP:        return "gdp";
      case CALENDAR_SECTOR_JOBS:       return "jobs";
      case CALENDAR_SECTOR_PRICES:     return "prices";
      case CALENDAR_SECTOR_MONEY:      return "money";
      case CALENDAR_SECTOR_TRADE:      return "trade";
      case CALENDAR_SECTOR_GOVERNMENT: return "government";
      case CALENDAR_SECTOR_BUSINESS:   return "business";
      case CALENDAR_SECTOR_CONSUMER:   return "consumer";
      case CALENDAR_SECTOR_HOUSING:    return "housing";
      case CALENDAR_SECTOR_TAXES:      return "taxes";
      case CALENDAR_SECTOR_HOLIDAYS:   return "holidays";
   }
   return StringFormat("unknown_%d", (int)v);
}

string FrequencyToStr(const ENUM_CALENDAR_EVENT_FREQUENCY v)
{
   switch(v)
   {
      case CALENDAR_FREQUENCY_NONE:    return "none";
      case CALENDAR_FREQUENCY_WEEK:    return "week";
      case CALENDAR_FREQUENCY_MONTH:   return "month";
      case CALENDAR_FREQUENCY_QUARTER: return "quarter";
      case CALENDAR_FREQUENCY_YEAR:    return "year";
      case CALENDAR_FREQUENCY_DAY:     return "day";
   }
   return StringFormat("unknown_%d", (int)v);
}

string TimeModeToStr(const ENUM_CALENDAR_EVENT_TIMEMODE v)
{
   switch(v)
   {
      case CALENDAR_TIMEMODE_DATETIME:  return "datetime";
      case CALENDAR_TIMEMODE_DATE:      return "all_day";
      case CALENDAR_TIMEMODE_NOTIME:    return "no_time";
      case CALENDAR_TIMEMODE_TENTATIVE: return "tentative";
   }
   return StringFormat("unknown_%d", (int)v);
}

string UnitToStr(const ENUM_CALENDAR_EVENT_UNIT v)
{
   switch(v)
   {
      case CALENDAR_UNIT_NONE:      return "none";
      case CALENDAR_UNIT_PERCENT:   return "percent";
      case CALENDAR_UNIT_CURRENCY:  return "currency";
      case CALENDAR_UNIT_HOUR:      return "hour";
      case CALENDAR_UNIT_JOB:       return "job";
      case CALENDAR_UNIT_RIG:       return "rig";
      case CALENDAR_UNIT_USD:       return "usd";
      case CALENDAR_UNIT_PEOPLE:    return "people";
      case CALENDAR_UNIT_MORTGAGE:  return "mortgage";
      case CALENDAR_UNIT_VOTE:      return "vote";
      case CALENDAR_UNIT_BARREL:    return "barrel";
      case CALENDAR_UNIT_CUBICFEET: return "cubicfeet";
      case CALENDAR_UNIT_POSITION:  return "position";
      case CALENDAR_UNIT_BUILDING:  return "building";
   }
   return StringFormat("unknown_%d", (int)v);
}

string MultiplierToStr(const ENUM_CALENDAR_EVENT_MULTIPLIER v)
{
   switch(v)
   {
      case CALENDAR_MULTIPLIER_NONE:      return "none";
      case CALENDAR_MULTIPLIER_THOUSANDS: return "thousands";
      case CALENDAR_MULTIPLIER_MILLIONS:  return "millions";
      case CALENDAR_MULTIPLIER_BILLIONS:  return "billions";
      case CALENDAR_MULTIPLIER_TRILLIONS: return "trillions";
   }
   return StringFormat("unknown_%d", (int)v);
}

string ImpactToStr(const ENUM_CALENDAR_EVENT_IMPACT v)
{
   switch(v)
   {
      case CALENDAR_IMPACT_NA:       return "na";
      case CALENDAR_IMPACT_POSITIVE: return "positive";
      case CALENDAR_IMPACT_NEGATIVE: return "negative";
   }
   return StringFormat("unknown_%d", (int)v);
}

//====================================================================
//  UTILITY
//====================================================================

//--- datetime -> "YYYY-MM-DDTHH:MM:SS" (MQL5 produce "YYYY.MM.DD HH:MI:SS")
string FormatIso(const datetime t)
{
   if(t <= 0) return "";
   string s = TimeToString(t, TIME_DATE | TIME_SECONDS);
   StringReplace(s, ".", "-");
   StringReplace(s, " ", "T");
   return s;
}

string FormatDay(const datetime t)
{
   if(t <= 0) return "";
   string s = TimeToString(t, TIME_DATE);
   StringReplace(s, ".", "-");
   return s;
}

int YearOf(const datetime t) { MqlDateTime d; TimeToStruct(t, d); return d.year; }

//--- Aggiunge mesi a una data mantenendo giorno 1 (i blocchi sono allineati al mese)
datetime AddMonths(const datetime t, const int months)
{
   MqlDateTime d;
   TimeToStruct(t, d);
   int total = d.year * 12 + (d.mon - 1) + months;
   d.year = total / 12;
   d.mon  = total % 12 + 1;
   d.day  = 1; d.hour = 0; d.min = 0; d.sec = 0;
   return StructToTime(d);
}

//--- Valore grezzo -> stringa numerica, oppure "" se assente (LONG_MIN)
string RawToNumStr(const long raw, const int digits)
{
   if(raw == LONG_MIN) return "";
   int dg = (digits >= 0 && digits <= 8) ? digits : 6;
   return DoubleToString((double)raw / VAL_SCALE, dg);
}

//--- Escape JSON conforme a RFC 8259
string JsonEsc(const string s)
{
   string o = "";
   int n = StringLen(s);
   for(int i = 0; i < n; i++)
   {
      ushort c = StringGetCharacter(s, i);
      if     (c == '"')  o += "\\\"";
      else if(c == '\\') o += "\\\\";
      else if(c == '\n') o += "\\n";
      else if(c == '\r') o += "\\r";
      else if(c == '\t') o += "\\t";
      else if(c < 0x20)  o += StringFormat("\\u%04x", c);
      else               o += ShortToString(c);
   }
   return o;
}

//--- Campo JSON stringa: "" diventa null, mai una stringa vuota mascherata
string JsonStr(const string s)
{
   if(StringLen(s) == 0) return "null";
   return "\"" + JsonEsc(s) + "\"";
}

//--- Campo JSON numerico: "" diventa null
string JsonNum(const string s)
{
   if(StringLen(s) == 0) return "null";
   return s;
}

//--- Escape CSV: virgolette raddoppiate, quoting se necessario
string CsvEsc(const string s)
{
   if(StringFind(s, "\"") < 0 && StringFind(s, ",") < 0 &&
      StringFind(s, "\n") < 0 && StringFind(s, "\r") < 0)
      return s;
   string o = s;
   StringReplace(o, "\"", "\"\"");
   StringReplace(o, "\r", " ");
   StringReplace(o, "\n", " ");
   return "\"" + o + "\"";
}

//--- time_utc: prodotto solo se l'utente ha dichiarato l'offset del server.
//    Senza offset dichiarato il campo resta null: non si inventa una conversione
//    che sarebbe comunque errata attraverso i cambi di ora legale.
string UtcIso(const datetime serverTime)
{
   if(InpServerGmtOffset > 90.0) return "";
   return FormatIso(serverTime - (int)MathRound(InpServerGmtOffset * 3600.0));
}

//====================================================================
//  CACHE METADATI
//====================================================================

//--- Restituisce i metadati evento, interrogando il terminale una sola volta per id.
//    La cache e' tenuta ordinata per id e cercata in binaria: con ~10^5 valori e
//    ~10^3 eventi distinti una scansione lineare costerebbe ~10^8 confronti.
bool GetEventCached(const ulong eventId, MqlCalendarEvent &out)
{
   int n = ArraySize(g_eventCache);

   //--- ricerca binaria
   int lo = 0, hi = n - 1, pos = n;
   while(lo <= hi)
   {
      int mid = (lo + hi) >> 1;
      if(g_eventCache[mid].id == eventId)
      {
         out = g_eventCache[mid].ev;
         return g_eventCache[mid].ok;
      }
      if(g_eventCache[mid].id < eventId) lo = mid + 1;
      else                             { hi = mid - 1; }
   }
   pos = lo;   // punto di inserimento che mantiene l'ordine

   EventCache rec;
   rec.id = eventId;
   ZeroMemory(rec.ev);              // se il lookup fallisce, ev non viene scritto
   rec.ok = CalendarEventById(eventId, rec.ev);
   if(!rec.ok) g_eventLookupFail++;

   ArrayResize(g_eventCache, n + 1, 512);
   for(int i = n; i > pos; i--) g_eventCache[i] = g_eventCache[i - 1];
   g_eventCache[pos] = rec;

   out = rec.ev;
   return rec.ok;
}

//--- Restituisce i metadati paese, interrogando il terminale una sola volta per id
bool GetCountryCached(const ulong countryId, MqlCalendarCountry &out)
{
   int n = ArraySize(g_countryCache);
   for(int i = 0; i < n; i++)
      if(g_countryCache[i].id == countryId)
      {
         out = g_countryCache[i].ct;
         return g_countryCache[i].ok;
      }

   CountryCache rec;
   rec.id = countryId;
   rec.ok = CalendarCountryById(countryId, rec.ct);

   ArrayResize(g_countryCache, n + 1);
   g_countryCache[n] = rec;

   out = rec.ct;
   return rec.ok;
}

//====================================================================
//  ACQUISIZIONE
//====================================================================

//--- Compone un CalRecord risolvendo evento e paese. false se l'evento è filtrato.
bool BuildRecord(const MqlCalendarValue &v, CalRecord &r)
{
   MqlCalendarEvent   ev;
   MqlCalendarCountry ct;

   bool evOk = GetEventCached(v.event_id, ev);

   r.value_id         = v.id;
   r.event_id         = v.event_id;
   r.time_server      = v.time;
   r.period           = v.period;
   r.revision         = v.revision;
   r.actual_raw       = v.actual_value;
   r.forecast_raw     = v.forecast_value;
   r.prev_raw         = v.prev_value;
   r.revised_prev_raw = v.revised_prev_value;
   r.impact_type      = ImpactToStr(v.impact_type);

   if(evOk)
   {
      r.event_name = ev.name;
      r.event_code = ev.event_code;
      r.event_type = TypeToStr(ev.type);
      r.sector     = SectorToStr(ev.sector);
      r.frequency  = FrequencyToStr(ev.frequency);
      r.time_mode  = TimeModeToStr(ev.time_mode);
      r.importance = ImportanceToStr(ev.importance);
      r.unit       = UnitToStr(ev.unit);
      r.multiplier = MultiplierToStr(ev.multiplier);
      r.digits     = (int)ev.digits;
      r.source_url = ev.source_url;
      r.country_id = ev.country_id;

      if(GetCountryCached(ev.country_id, ct))
      {
         r.country_name = ct.name;
         r.country_code = ct.code;
         r.currency     = ct.currency;
      }
      else { r.country_name = ""; r.country_code = ""; r.currency = ""; }
   }
   else
   {
      // Evento non risolvibile: i campi restano vuoti -> null in export.
      r.event_name = ""; r.event_code = ""; r.event_type = "";
      r.sector = ""; r.frequency = ""; r.time_mode = "";
      r.importance = ""; r.unit = ""; r.multiplier = "";
      r.digits = 6; r.source_url = ""; r.country_id = 0;
      r.country_name = ""; r.country_code = ""; r.currency = "";
   }

   if(InpOnlyWithActual && v.actual_value == LONG_MIN) return false;
   return true;
}

//--- Acquisisce un singolo blocco. ok=false segnala un errore recuperabile.
int FetchChunk(const datetime from, const datetime to, bool &ok, string &reason)
{
   MqlCalendarValue vals[];
   ResetLastError();

   // NULL e' di tipo void: in un operatore ternario non e' un operando valido e,
   // se la build lo tollera, la promozione di tipo lo trasformerebbe nella stringa
   // "0" — filtro inesistente, zero righe, export vuoto senza errore. Assegnazione
   // esplicita, non ternario.
   string cc = NULL;   if(StringLen(InpCountryCode) > 0) cc  = InpCountryCode;
   string cur = NULL;  if(StringLen(InpCurrency)    > 0) cur = InpCurrency;

   int n = CalendarValueHistory(vals, from, to, cc, cur);
   int err = GetLastError();

   if(n < 0)
   {
      ok = false;
      if(err == ERR_CAL_MORE)         reason = "ERR_CALENDAR_MORE_DATA (5400): blocco troppo ampio";
      else if(err == ERR_CAL_TIMEOUT) reason = "ERR_CALENDAR_TIMEOUT (5401): timeout del terminale";
      else                            reason = StringFormat("errore %d", err);
      return -1;
   }

   ok = true; reason = "";
   int added = 0;
   int base  = ArraySize(g_records);
   // reserve: CalRecord e' una struttura complessa (15 stringhe). Senza riserva ogni
   // blocco riallocherebbe l'intero array con copia profonda di tutte le stringhe.
   ArrayResize(g_records, base + n, 65536);

   for(int i = 0; i < n; i++)
   {
      CalRecord r;
      if(!BuildRecord(vals[i], r)) continue;
      g_records[base + added] = r;
      added++;
   }
   ArrayResize(g_records, base + added);
   g_rawCollected += n;
   return added;
}

//--- Acquisisce un blocco dimezzandolo finché il terminale non lo accetta.
void CollectAdaptive(const datetime from, const datetime to, const int depth)
{
   bool   ok;
   string reason;
   int    got = FetchChunk(from, to, ok, reason);

   if(ok)
   {
      ChunkStatus cs;
      cs.from = from; cs.to = to; cs.values = got;
      cs.state  = (got > 0) ? "completed" : "empty_verified";
      cs.reason = "";
      int k = ArraySize(g_chunks); ArrayResize(g_chunks, k + 1); g_chunks[k] = cs;

      PrintFormat("  %s -> %s : %d valori [%s]",
                  FormatDay(from), FormatDay(to), got, cs.state);
      if(InpPauseMs > 0) Sleep(InpPauseMs);
      return;
   }

   // Errore recuperabile: se il blocco è ancora divisibile, dimezzalo.
   long span = (long)(to - from);
   if(span > (long)MIN_CHUNK_DAYS * 86400 && depth < 12)
   {
      datetime mid = (datetime)(from + span / 2);
      PrintFormat("  %s -> %s : %s, divido", FormatDay(from), FormatDay(to), reason);
      CollectAdaptive(from, mid, depth + 1);
      CollectAdaptive(mid,  to,  depth + 1);
      return;
   }

   // Blocco minimo non divisibile: ritenta, poi dichiara il fallimento.
   for(int attempt = 1; attempt <= InpMaxRetries; attempt++)
   {
      Sleep(250 * attempt);
      got = FetchChunk(from, to, ok, reason);
      if(ok)
      {
         ChunkStatus cs;
         cs.from = from; cs.to = to; cs.values = got;
         cs.state  = (got > 0) ? "completed" : "empty_verified";
         cs.reason = StringFormat("recuperato al tentativo %d", attempt);
         int k = ArraySize(g_chunks); ArrayResize(g_chunks, k + 1); g_chunks[k] = cs;
         return;
      }
   }

   ChunkStatus cs;
   cs.from = from; cs.to = to; cs.values = 0;
   cs.state = "failed_explained"; cs.reason = reason;
   int k = ArraySize(g_chunks); ArrayResize(g_chunks, k + 1); g_chunks[k] = cs;
   PrintFormat("  FALLITO %s -> %s : %s", FormatDay(from), FormatDay(to), reason);
}

//--- Prima data realmente disponibile: ricerca binaria su anni, poi mesi.
//    Misurata, non assunta.
datetime DiscoverEarliest(const datetime upperBound)
{
   MqlCalendarValue probe[];
   int lo = 1990, hi = YearOf(upperBound);
   int firstYear = -1;

   for(int y = lo; y <= hi; y++)
   {
      MqlDateTime d; ZeroMemory(d);
      d.year = y; d.mon = 1; d.day = 1;
      datetime a = StructToTime(d);
      d.year = y + 1;
      datetime b = StructToTime(d);

      ResetLastError();
      int n = CalendarValueHistory(probe, a, b, NULL, NULL);

      // -1 = errore, 0 = anno realmente vuoto. Confonderli farebbe partire
      // l'export un anno dopo dichiarando di aver "misurato" la prima data.
      if(n < 0)
      {
         g_discoveryErrors++;
         PrintFormat("  discovery: errore %d sull'anno %d, anno non concludente",
                     GetLastError(), y);
         continue;
      }
      if(n > 0) { firstYear = y; break; }
   }

   if(firstYear < 0) return 0;

   for(int m = 1; m <= 12; m++)
   {
      MqlDateTime d; ZeroMemory(d);
      d.year = firstYear; d.mon = m; d.day = 1;
      datetime a = StructToTime(d);
      datetime b = AddMonths(a, 1);

      ResetLastError();
      int n = CalendarValueHistory(probe, a, b, NULL, NULL);
      if(n < 0)
      {
         g_discoveryErrors++;
         PrintFormat("  discovery: errore %d sul mese %04d-%02d", GetLastError(), firstYear, m);
         continue;
      }
      if(n > 0)
      {
         datetime earliest = probe[0].time;
         for(int i = 1; i < n; i++) if(probe[i].time < earliest) earliest = probe[i].time;
         return earliest;
      }
   }

   MqlDateTime d; ZeroMemory(d);
   d.year = firstYear; d.mon = 1; d.day = 1;
   return StructToTime(d);
}

//====================================================================
//  ORDINAMENTO E DEDUP
//====================================================================

//--- Indici ordinati. Si ordinano gli interi, non i record: CalRecord porta 15
//    stringhe e un quicksort che scambia record farebbe milioni di copie profonde.
int g_idx[];

//--- true se il record di indice ia precede quello di indice ib
bool IdxLess(const int ia, const int ib)
{
   if(g_records[ia].time_server != g_records[ib].time_server)
      return (g_records[ia].time_server < g_records[ib].time_server);
   return (g_records[ia].value_id < g_records[ib].value_id);
}

//--- Quicksort sugli indici: ricorsione sulla partizione piccola, iterazione
//    sulla grande, cosi' la profondita' di stack resta O(log n) anche nel caso
//    peggiore (l'output dei blocchi e' quasi ordinato).
void QuickSortIdx(int lo, int hi)
{
   while(lo < hi)
   {
      int i = lo, j = hi;
      int pivot = g_idx[(lo + hi) >> 1];
      while(i <= j)
      {
         while(IdxLess(g_idx[i], pivot)) i++;
         while(IdxLess(pivot, g_idx[j])) j--;
         if(i <= j)
         {
            int t = g_idx[i]; g_idx[i] = g_idx[j]; g_idx[j] = t;
            i++; j--;
         }
      }
      if(j - lo < hi - i) { if(lo < j) QuickSortIdx(lo, j); lo = i; }
      else                { if(i < hi) QuickSortIdx(i, hi); hi = j; }
   }
}

//--- Applica in place la permutazione g_idx (decomposizione in cicli): nessun
//    array di appoggio, quindi nessun raddoppio della memoria occupata.
void ApplyPermutation(void)
{
   int n = ArraySize(g_records);
   bool done[];
   ArrayResize(done, n);
   ArrayInitialize(done, false);

   for(int i = 0; i < n; i++)
   {
      if(done[i] || g_idx[i] == i) { done[i] = true; continue; }

      CalRecord tmp = g_records[i];
      int j = i;
      while(true)
      {
         int k = g_idx[j];          // indice sorgente che deve finire in posizione j
         done[j] = true;
         if(k == i) { g_records[j] = tmp; break; }
         g_records[j] = g_records[k];
         j = k;
      }
   }
}

//--- Ordina per (time_server, value_id) e rimuove i duplicati per value_id.
//    I blocchi confinanti condividono l'estremo, quindi le sovrapposizioni
//    sono attese e non sono un errore.
void SortAndDedup(void)
{
   int n = ArraySize(g_records);
   if(n < 2) return;

   ArrayResize(g_idx, n);
   for(int i = 0; i < n; i++) g_idx[i] = i;

   QuickSortIdx(0, n - 1);
   ApplyPermutation();
   ArrayFree(g_idx);

   int w = 1;
   for(int i = 1; i < n; i++)
   {
      if(g_records[i].value_id == g_records[w - 1].value_id) { g_dupRemoved++; continue; }
      if(w != i) g_records[w] = g_records[i];
      w++;
   }
   ArrayResize(g_records, w);
}

//====================================================================
//  EXPORT
//====================================================================

//--- Intestazione CSV. Funzione e non costante globale: in MQL5 una const string
//    globale con concatenazione implicita non e' portabile fra build.
string CsvHeader(void)
{
   return "value_id,event_id,time_server,time_utc,period,revision,"
          "country_code,country_name,currency,event_name,event_code,event_type,"
          "sector,importance,frequency,time_mode,unit,multiplier,digits,"
          "actual,forecast,previous,revised_previous,"
          "actual_raw,forecast_raw,previous_raw,revised_previous_raw,"
          "impact_type,source_url\n";
}

string RecordToCsv(const CalRecord &r)
{
   string a  = RawToNumStr(r.actual_raw,       r.digits);
   string f  = RawToNumStr(r.forecast_raw,     r.digits);
   string p  = RawToNumStr(r.prev_raw,         r.digits);
   string rp = RawToNumStr(r.revised_prev_raw, r.digits);

   string out = "";
   out += IntegerToString((long)r.value_id) + ",";
   out += IntegerToString((long)r.event_id) + ",";
   out += FormatIso(r.time_server) + ",";
   out += UtcIso(r.time_server) + ",";
   out += FormatIso(r.period) + ",";
   out += IntegerToString(r.revision) + ",";
   out += CsvEsc(r.country_code) + ",";
   out += CsvEsc(r.country_name) + ",";
   out += CsvEsc(r.currency) + ",";
   out += CsvEsc(r.event_name) + ",";
   out += CsvEsc(r.event_code) + ",";
   out += CsvEsc(r.event_type) + ",";
   out += CsvEsc(r.sector) + ",";
   out += CsvEsc(r.importance) + ",";
   out += CsvEsc(r.frequency) + ",";
   out += CsvEsc(r.time_mode) + ",";
   out += CsvEsc(r.unit) + ",";
   out += CsvEsc(r.multiplier) + ",";
   out += IntegerToString(r.digits) + ",";
   out += a + "," + f + "," + p + "," + rp + ",";
   out += (r.actual_raw       == LONG_MIN ? "" : IntegerToString(r.actual_raw))       + ",";
   out += (r.forecast_raw     == LONG_MIN ? "" : IntegerToString(r.forecast_raw))     + ",";
   out += (r.prev_raw         == LONG_MIN ? "" : IntegerToString(r.prev_raw))         + ",";
   out += (r.revised_prev_raw == LONG_MIN ? "" : IntegerToString(r.revised_prev_raw)) + ",";
   out += CsvEsc(r.impact_type) + ",";
   out += CsvEsc(r.source_url) + "\n";
   return out;
}

string RecordToJson(const CalRecord &r)
{
   string a  = RawToNumStr(r.actual_raw,       r.digits);
   string f  = RawToNumStr(r.forecast_raw,     r.digits);
   string p  = RawToNumStr(r.prev_raw,         r.digits);
   string rp = RawToNumStr(r.revised_prev_raw, r.digits);

   string o = "{";
   o += "\"value_id\":"     + IntegerToString((long)r.value_id) + ",";
   o += "\"event_id\":"     + IntegerToString((long)r.event_id) + ",";
   o += "\"time_server\":"  + JsonStr(FormatIso(r.time_server)) + ",";
   o += "\"time_utc\":"     + JsonStr(UtcIso(r.time_server)) + ",";
   o += "\"period\":"       + JsonStr(FormatIso(r.period)) + ",";
   o += "\"revision\":"     + IntegerToString(r.revision) + ",";
   o += "\"country_code\":" + JsonStr(r.country_code) + ",";
   o += "\"country_name\":" + JsonStr(r.country_name) + ",";
   o += "\"currency\":"     + JsonStr(r.currency) + ",";
   o += "\"event_name\":"   + JsonStr(r.event_name) + ",";
   o += "\"event_code\":"   + JsonStr(r.event_code) + ",";
   o += "\"event_type\":"   + JsonStr(r.event_type) + ",";
   o += "\"sector\":"       + JsonStr(r.sector) + ",";
   o += "\"importance\":"   + JsonStr(r.importance) + ",";
   o += "\"frequency\":"    + JsonStr(r.frequency) + ",";
   o += "\"time_mode\":"    + JsonStr(r.time_mode) + ",";
   o += "\"unit\":"         + JsonStr(r.unit) + ",";
   o += "\"multiplier\":"   + JsonStr(r.multiplier) + ",";
   o += "\"digits\":"       + IntegerToString(r.digits) + ",";
   o += "\"actual\":"           + JsonNum(a) + ",";
   o += "\"forecast\":"         + JsonNum(f) + ",";
   o += "\"previous\":"         + JsonNum(p) + ",";
   o += "\"revised_previous\":" + JsonNum(rp) + ",";
   o += "\"actual_raw\":"           + (r.actual_raw       == LONG_MIN ? "null" : IntegerToString(r.actual_raw))       + ",";
   o += "\"forecast_raw\":"         + (r.forecast_raw     == LONG_MIN ? "null" : IntegerToString(r.forecast_raw))     + ",";
   o += "\"previous_raw\":"         + (r.prev_raw         == LONG_MIN ? "null" : IntegerToString(r.prev_raw))         + ",";
   o += "\"revised_previous_raw\":" + (r.revised_prev_raw == LONG_MIN ? "null" : IntegerToString(r.revised_prev_raw)) + ",";
   o += "\"impact_type\":" + JsonStr(r.impact_type) + ",";
   o += "\"source_url\":"  + JsonStr(r.source_url);
   o += "}";
   return o;
}

//--- Scrive CSV, JSON e JSONL. Restituisce il numero di file creati.
int ExportAll(const string prefix)
{
   int n = ArraySize(g_records);
   int written = 0;

   if(InpExportCSV)
   {
      CUtf8Writer w;
      if(w.Open(prefix + ".csv", true))          // BOM: compatibilità Excel
      {
         w.Write(CsvHeader());
         string buf = "";
         for(int i = 0; i < n; i++)
         {
            buf += RecordToCsv(g_records[i]);
            if((i % 500) == 499) { w.Write(buf); buf = ""; }
         }
         w.Write(buf);
         w.Close();
         written++;
         PrintFormat("CSV   : %s.csv (%d righe)", prefix, n);
      }
   }

   if(InpExportJSON)
   {
      CUtf8Writer w;
      if(w.Open(prefix + ".json", false))
      {
         w.Write("[\n");
         string buf = "";
         for(int i = 0; i < n; i++)
         {
            buf += RecordToJson(g_records[i]);
            if(i < n - 1) buf += ",";
            buf += "\n";
            if((i % 500) == 499) { w.Write(buf); buf = ""; }
         }
         w.Write(buf);
         w.Write("]\n");
         w.Close();
         written++;
         PrintFormat("JSON  : %s.json (%d record)", prefix, n);
      }
   }

   if(InpExportJSONL)
   {
      CUtf8Writer w;
      if(w.Open(prefix + ".jsonl", false))
      {
         string buf = "";
         for(int i = 0; i < n; i++)
         {
            buf += RecordToJson(g_records[i]) + "\n";
            if((i % 500) == 499) { w.Write(buf); buf = ""; }
         }
         w.Write(buf);
         w.Close();
         written++;
         PrintFormat("JSONL : %s.jsonl (%d righe)", prefix, n);
      }
   }
   return written;
}

//--- Metadati + report di copertura: conteggi per anno, valuta, importanza,
//    stato di ogni blocco. Nessun numero stimato: tutto contato.
void ExportReport(const string prefix, const datetime from, const datetime to,
                  const uint elapsedMs)
{
   int n = ArraySize(g_records);

   //--- conteggi per anno
   int years[]; int yearCnt[];
   //--- conteggi per valuta
   string curs[]; int curCnt[];
   //--- conteggi per importanza
   string imps[]; int impCnt[];

   int withActual = 0, withForecast = 0, allDay = 0, tentative = 0;

   for(int i = 0; i < n; i++)
   {
      int y = YearOf(g_records[i].time_server);
      int k = -1;
      for(int j = 0; j < ArraySize(years); j++) if(years[j] == y) { k = j; break; }
      if(k < 0) { k = ArraySize(years); ArrayResize(years, k+1); ArrayResize(yearCnt, k+1);
                  years[k] = y; yearCnt[k] = 0; }
      yearCnt[k]++;

      string c = g_records[i].currency;
      k = -1;
      for(int j = 0; j < ArraySize(curs); j++) if(curs[j] == c) { k = j; break; }
      if(k < 0) { k = ArraySize(curs); ArrayResize(curs, k+1); ArrayResize(curCnt, k+1);
                  curs[k] = c; curCnt[k] = 0; }
      curCnt[k]++;

      string im = g_records[i].importance;
      k = -1;
      for(int j = 0; j < ArraySize(imps); j++) if(imps[j] == im) { k = j; break; }
      if(k < 0) { k = ArraySize(imps); ArrayResize(imps, k+1); ArrayResize(impCnt, k+1);
                  imps[k] = im; impCnt[k] = 0; }
      impCnt[k]++;

      if(g_records[i].actual_raw   != LONG_MIN) withActual++;
      if(g_records[i].forecast_raw != LONG_MIN) withForecast++;
      if(g_records[i].time_mode == "all_day")   allDay++;
      if(g_records[i].time_mode == "tentative") tentative++;
   }

   int chCompleted = 0, chEmpty = 0, chFailed = 0;
   for(int i = 0; i < ArraySize(g_chunks); i++)
   {
      if(g_chunks[i].state == "completed")        chCompleted++;
      else if(g_chunks[i].state == "empty_verified") chEmpty++;
      else                                        chFailed++;
   }

   CUtf8Writer w;
   if(!w.Open(prefix + "_report.json", false)) return;

   w.Write("{\n");
   w.Write("  \"schema\": \"mt5-calendar-export/1\",\n");
   w.Write("  \"parser_version\": \"" + PARSER_VERSION + "\",\n");
   w.Write("  \"source\": \"MetaTrader 5 terminal economic calendar (MQL5 Calendar API)\",\n");
   w.Write("  \"generated_at_server\": \"" + FormatIso(TimeCurrent()) + "\",\n");
   w.Write("  \"generated_at_gmt\": \"" + FormatIso(TimeGMT()) + "\",\n");
   w.Write("  \"terminal\": {\n");
   w.Write("    \"company\": " + JsonStr(AccountInfoString(ACCOUNT_COMPANY)) + ",\n");
   w.Write("    \"server\": "  + JsonStr(AccountInfoString(ACCOUNT_SERVER)) + ",\n");
   w.Write("    \"build\": "   + IntegerToString(TerminalInfoInteger(TERMINAL_BUILD)) + "\n");
   w.Write("  },\n");
   w.Write("  \"timezone\": {\n");
   w.Write("    \"times_are_in\": \"trade server timezone\",\n");
   w.Write("    \"declared_gmt_offset_hours\": " +
           (InpServerGmtOffset > 90.0 ? "null" : DoubleToString(InpServerGmtOffset, 2)) + ",\n");
   w.Write("    \"observed_offset_at_export_hours\": " +
           DoubleToString((double)(TimeCurrent() - TimeGMT()) / 3600.0, 2) + ",\n");
   w.Write("    \"note\": \"time_utc e' valorizzato solo se declared_gmt_offset_hours e' impostato. L'offset osservato vale al momento dell'export e non si applica retroattivamente attraverso i cambi di ora legale.\"\n");
   w.Write("  },\n");
   w.Write("  \"request\": {\n");
   w.Write("    \"from\": \"" + FormatIso(from) + "\",\n");
   w.Write("    \"to\": \""   + FormatIso(to)   + "\",\n");
   w.Write("    \"country_code\": " + JsonStr(InpCountryCode) + ",\n");
   w.Write("    \"currency\": "     + JsonStr(InpCurrency) + ",\n");
   w.Write("    \"only_with_actual\": " + (InpOnlyWithActual ? "true" : "false") + "\n");
   w.Write("  },\n");
   w.Write("  \"coverage\": {\n");
   w.Write("    \"records\": "         + IntegerToString(n) + ",\n");
   w.Write("    \"raw_collected\": "   + IntegerToString(g_rawCollected) + ",\n");
   w.Write("    \"duplicates_removed\": " + IntegerToString(g_dupRemoved) + ",\n");
   w.Write("    \"event_lookup_failures\": " + IntegerToString(g_eventLookupFail) + ",\n");
   w.Write("    \"discovery_probe_errors\": " + IntegerToString(g_discoveryErrors) + ",\n");
   w.Write("    \"first_record\": " + JsonStr(n > 0 ? FormatIso(g_records[0].time_server) : "") + ",\n");
   w.Write("    \"last_record\": "  + JsonStr(n > 0 ? FormatIso(g_records[n-1].time_server) : "") + ",\n");
   w.Write("    \"with_actual\": "   + IntegerToString(withActual) + ",\n");
   w.Write("    \"with_forecast\": " + IntegerToString(withForecast) + ",\n");
   w.Write("    \"all_day\": "       + IntegerToString(allDay) + ",\n");
   w.Write("    \"tentative\": "     + IntegerToString(tentative) + ",\n");
   w.Write("    \"chunks_completed\": "      + IntegerToString(chCompleted) + ",\n");
   w.Write("    \"chunks_empty_verified\": " + IntegerToString(chEmpty) + ",\n");
   w.Write("    \"chunks_failed\": "         + IntegerToString(chFailed) + ",\n");
   w.Write("    \"elapsed_ms\": " + IntegerToString((long)elapsedMs) + "\n");
   w.Write("  },\n");

   w.Write("  \"by_year\": {");
   for(int i = 0; i < ArraySize(years); i++)
   {
      if(i > 0) w.Write(",");
      w.Write("\"" + IntegerToString(years[i]) + "\":" + IntegerToString(yearCnt[i]));
   }
   w.Write("},\n");

   w.Write("  \"by_currency\": {");
   for(int i = 0; i < ArraySize(curs); i++)
   {
      if(i > 0) w.Write(",");
      w.Write(JsonStr(StringLen(curs[i]) > 0 ? curs[i] : "(none)") + ":" + IntegerToString(curCnt[i]));
   }
   w.Write("},\n");

   w.Write("  \"by_importance\": {");
   for(int i = 0; i < ArraySize(imps); i++)
   {
      if(i > 0) w.Write(",");
      w.Write(JsonStr(StringLen(imps[i]) > 0 ? imps[i] : "(none)") + ":" + IntegerToString(impCnt[i]));
   }
   w.Write("},\n");

   w.Write("  \"chunks\": [\n");
   for(int i = 0; i < ArraySize(g_chunks); i++)
   {
      w.Write("    {\"from\":\"" + FormatDay(g_chunks[i].from) + "\",\"to\":\"" + FormatDay(g_chunks[i].to) +
              "\",\"values\":" + IntegerToString(g_chunks[i].values) +
              ",\"state\":\"" + g_chunks[i].state + "\",\"reason\":" + JsonStr(g_chunks[i].reason) + "}");
      if(i < ArraySize(g_chunks) - 1) w.Write(",");
      w.Write("\n");
   }
   w.Write("  ]\n}\n");
   w.Close();

   PrintFormat("REPORT: %s_report.json", prefix);
}

//====================================================================
//  MAIN
//====================================================================
void OnStart()
{
   uint t0 = GetTickCount();

   Print("=====================================================");
   Print(" CalendarHistoryExporter ", PARSER_VERSION);
   Print(" Fonte: calendario del terminale MT5 (API MQL5)");
   Print("=====================================================");

   //--- Il calendario non e' disponibile in tutti i contesti
   if(!TerminalInfoInteger(TERMINAL_CONNECTED))
      Print("ATTENZIONE: terminale non connesso. Il calendario potrebbe essere incompleto.");

   datetime to = (InpDateTo > 0) ? InpDateTo : TimeCurrent();
   datetime from;

   if(InpAutoDiscoverStart)
   {
      Print("Discovery della prima data disponibile...");
      from = DiscoverEarliest(to);
      if(from == 0)
      {
         Print("ESITO: BLOCCATO — il calendario del terminale non ha restituito alcun valore.");
         Print("Cause possibili: calendario disabilitato dal broker, terminale offline,");
         Print("oppure esecuzione nello Strategy Tester (il calendario e' limitato in tester).");
         return;
      }
      PrintFormat("Prima data misurata: %s", FormatIso(from));
   }
   else
   {
      from = InpDateFrom;
      PrintFormat("Prima data richiesta: %s", FormatIso(from));
   }

   if(from >= to)
   {
      PrintFormat("ESITO: BLOCCATO — intervallo non valido (%s >= %s).",
                  FormatIso(from), FormatIso(to));
      return;
   }

   //--- Acquisizione a blocchi
   int months = (InpChunkMonths > 0) ? InpChunkMonths : 3;
   PrintFormat("Acquisizione %s -> %s, blocchi da %d mesi",
               FormatDay(from), FormatDay(to), months);

   datetime cur = from;
   while(cur < to)
   {
      datetime next = AddMonths(cur, months);
      if(next > to || next <= cur) next = to;
      CollectAdaptive(cur, next, 0);
      cur = next;
   }

   PrintFormat("Valori grezzi letti: %d", g_rawCollected);

   SortAndDedup();
   int n = ArraySize(g_records);

   if(n == 0)
   {
      Print("ESITO: BLOCCATO — 0 record dopo la deduplica. Nessun file prodotto.");
      return;
   }

   //--- Export
   string prefix = InpPrefix + "_" + FormatDay(g_records[0].time_server)
                             + "_" + FormatDay(g_records[n-1].time_server);
   StringReplace(prefix, "-", "");

   int files = ExportAll(prefix);
   ExportReport(prefix, from, to, GetTickCount() - t0);

   //--- Riepilogo operativo
   int chFailed = 0;
   for(int i = 0; i < ArraySize(g_chunks); i++)
      if(g_chunks[i].state == "failed_explained") chFailed++;

   Print("-----------------------------------------------------");
   PrintFormat("ESITO: %s", (chFailed == 0 ? "COMPLETATO" : "PARZIALE"));
   PrintFormat("Record totali        : %d", n);
   PrintFormat("Duplicati rimossi    : %d", g_dupRemoved);
   PrintFormat("Eventi non risolti   : %d", g_eventLookupFail);
   PrintFormat("Errori in discovery  : %d", g_discoveryErrors);
   PrintFormat("Prima data           : %s", FormatIso(g_records[0].time_server));
   PrintFormat("Ultima data          : %s", FormatIso(g_records[n-1].time_server));
   PrintFormat("Blocchi falliti      : %d", chFailed);
   PrintFormat("File creati          : %d + report", files);
   PrintFormat("Cartella             : %s", InpUseCommonFolder ? "Common\\Files" : "MQL5\\Files");
   PrintFormat("Durata               : %.1f s", (GetTickCount() - t0) / 1000.0);
   Print("-----------------------------------------------------");
   if(chFailed > 0)
      Print("Alcuni blocchi sono falliti: il dettaglio con il motivo e' in *_report.json -> chunks.");
}
//+------------------------------------------------------------------+
