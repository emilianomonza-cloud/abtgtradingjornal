//+------------------------------------------------------------------+
//|                                        MacroScenarioTypes.mqh     |
//|                        Macro Scenario Engine - tipi e parser JSON |
//+------------------------------------------------------------------+
//| Strutture condivise fra indicatore ed Expert Advisor + parser JSON |
//| minimale.                                                          |
//|                                                                    |
//| SCELTA TECNICA (richiesta dalla SEZIONE 6.1): invece di importare   |
//| una libreria JSON generica si implementa un parser dedicato allo    |
//| schema FISSO dell'endpoint /api/v1/mt5/snapshot. Motivi:            |
//|   * nessuna dipendenza esterna da installare o mantenere;           |
//|   * nessuna allocazione ricorsiva: su M1 il parsing costa < 1 ms;   |
//|   * lo schema e' versionato ("v": 1) e il backend ripulisce ogni    |
//|     stringa da virgolette e backslash, quindi non servono escape.   |
//| Se il campo "v" cambia, il bridge lo segnala invece di indovinare.  |
//+------------------------------------------------------------------+
#property copyright "Macro Scenario Engine"
#property strict

#define MSE_SCHEMA_VERSION 1

//+------------------------------------------------------------------+
//| Strutture dati                                                    |
//+------------------------------------------------------------------+
struct CurrencyScore
  {
   string            ccy;        // valuta (USD, EUR, XAU...)
   double            score;      // -100..+100
   double            quality;    // 0..1 qualita' dei dati
  };

struct PairScenario
  {
   string            pair;            // EURUSD
   double            bias;            // -100..+100
   string            bias_label;      // STRONG_BULLISH ... STRONG_BEARISH
   string            direction;       // RIALZO / RIBASSO / LATERALE
   double            p_base;          // probabilita' scenario base (%)
   double            p_alt_a;         // probabilita' alternativo A (%)
   double            p_alt_b;         // probabilita' alternativo B (%)
   string            confidence;      // ALTA / MEDIA / BASSA
   string            expires;         // scadenza scenario (ISO 8601 UTC)
   bool              rewritable;      // evento RED imminente
   int               minutes_to_red;  // minuti al prossimo RED (-1 = nessuno)
   string            red_title;       // descrizione del prossimo RED
   bool              no_trade;        // finestra di blocco attiva
   int               no_trade_left;   // minuti alla fine della finestra
   string            no_trade_event;  // evento che ha aperto la finestra
   string            action;          // suggerimento non vincolante
  };

struct CalendarEvent
  {
   string            ccy;
   string            title;
   string            impact;     // RED / ORANGE
   int               minutes;    // minuti mancanti
   string            ts;         // ISO 8601 UTC
  };

struct MacroSnapshot
  {
   bool              valid;
   string            error;
   int               version;
   string            generated_at;    // ISO 8601 UTC
   datetime          generated_time;  // convertito in datetime
   string            horizon;
   string            degraded;        // fonti non OK (elenco grezzo)
   CurrencyScore     currencies[];
   PairScenario      pairs[];
   CalendarEvent     events[];
  };

//+------------------------------------------------------------------+
//| Utility di parsing                                                |
//+------------------------------------------------------------------+

//--- Restituisce la posizione della chiave "nome": a partire da 'from'
int MseFindKey(const string src,const string key,const int from)
  {
   return StringFind(src,"\""+key+"\":",from);
  }

//--- Legge una stringa: "chiave":"valore"
string MseGetString(const string src,const string key,const int from=0)
  {
   int pos=MseFindKey(src,key,from);
   if(pos<0)
      return("");
   int start=StringFind(src,"\"",pos+StringLen(key)+3);
   if(start<0)
      return("");
   int end=StringFind(src,"\"",start+1);
   if(end<0)
      return("");
   return(StringSubstr(src,start+1,end-start-1));
  }

//--- Legge un numero: "chiave":12.34  (accetta anche interi e negativi)
double MseGetNumber(const string src,const string key,const double def,const int from=0)
  {
   int pos=MseFindKey(src,key,from);
   if(pos<0)
      return(def);
   int i=pos+StringLen(key)+3;
   int len=StringLen(src);
   while(i<len)
     {
      ushort c=StringGetCharacter(src,i);
      if(c==' '||c=='\n'||c=='\r'||c=='\t')
        {
         i++;
         continue;
        }
      break;
     }
   int start=i;
   while(i<len)
     {
      ushort c=StringGetCharacter(src,i);
      if((c>='0' && c<='9')||c=='-'||c=='+'||c=='.'||c=='e'||c=='E')
         i++;
      else
         break;
     }
   if(i==start)
      return(def);
   return(StringToDouble(StringSubstr(src,start,i-start)));
  }

//--- Estrae il contenuto grezzo di un array: "chiave":[ ... ]
string MseGetArray(const string src,const string key)
  {
   int pos=MseFindKey(src,key,0);
   if(pos<0)
      return("");
   int open=StringFind(src,"[",pos);
   if(open<0)
      return("");
   int depth=0;
   int len=StringLen(src);
   for(int i=open;i<len;i++)
     {
      ushort c=StringGetCharacter(src,i);
      if(c=='[')
         depth++;
      else
         if(c==']')
           {
            depth--;
            if(depth==0)
               return(StringSubstr(src,open+1,i-open-1));
           }
     }
   return("");
  }

//--- Spezza un array in oggetti {...} di primo livello
int MseSplitObjects(const string array_body,string &out[])
  {
   ArrayResize(out,0);
   int depth=0;
   int start=-1;
   int len=StringLen(array_body);
   for(int i=0;i<len;i++)
     {
      ushort c=StringGetCharacter(array_body,i);
      if(c=='{')
        {
         if(depth==0)
            start=i;
         depth++;
        }
      else
         if(c=='}')
           {
            depth--;
            if(depth==0 && start>=0)
              {
               int n=ArraySize(out);
               ArrayResize(out,n+1);
               out[n]=StringSubstr(array_body,start,i-start+1);
               start=-1;
              }
           }
     }
   return(ArraySize(out));
  }

//--- Converte "2026-08-15T19:12:34.123456Z" in datetime (UTC)
datetime MseParseIso(const string iso)
  {
   if(StringLen(iso)<19)
      return(0);
   string normalized=iso;
   StringReplace(normalized,"T"," ");
   string date_time=StringSubstr(normalized,0,19);
   StringReplace(date_time,"-",".");
   return(StringToTime(date_time));
  }

//--- Normalizza il simbolo del grafico: "EURUSD.m" -> "EURUSD"
string MseNormalizeSymbol(const string symbol)
  {
   string upper=symbol;
   StringToUpper(upper);
   string clean="";
   int len=StringLen(upper);
   for(int i=0;i<len && StringLen(clean)<6;i++)
     {
      ushort c=StringGetCharacter(upper,i);
      if((c>='A' && c<='Z')||(c>='0' && c<='9'))
         clean+=ShortToString(c);
      else
         break;
     }
   return(clean);
  }

//+------------------------------------------------------------------+
//| Parsing dello snapshot                                            |
//+------------------------------------------------------------------+
bool MseParseSnapshot(const string json,MacroSnapshot &snap)
  {
   snap.valid=false;
   snap.error="";
   ArrayResize(snap.currencies,0);
   ArrayResize(snap.pairs,0);
   ArrayResize(snap.events,0);

   if(StringLen(json)<20)
     {
      snap.error="Payload vuoto o troppo corto";
      return(false);
     }

   snap.version=(int)MseGetNumber(json,"v",0);
   if(snap.version!=MSE_SCHEMA_VERSION)
     {
      snap.error=StringFormat("Versione schema %d non supportata (attesa %d)",
                              snap.version,MSE_SCHEMA_VERSION);
      return(false);
     }

   snap.generated_at=MseGetString(json,"ts");
   snap.generated_time=MseParseIso(snap.generated_at);
   snap.horizon=MseGetString(json,"hz");
   snap.degraded=MseGetArray(json,"fonti_degradate");

   //--- valute
   string ccy_body=MseGetArray(json,"ccy");
   string items[];
   int count=MseSplitObjects(ccy_body,items);
   ArrayResize(snap.currencies,count);
   for(int i=0;i<count;i++)
     {
      snap.currencies[i].ccy=MseGetString(items[i],"c");
      snap.currencies[i].score=MseGetNumber(items[i],"s",0.0);
      snap.currencies[i].quality=MseGetNumber(items[i],"q",0.0);
     }

   //--- coppie
   string pair_body=MseGetArray(json,"pairs");
   count=MseSplitObjects(pair_body,items);
   ArrayResize(snap.pairs,count);
   for(int i=0;i<count;i++)
     {
      snap.pairs[i].pair=MseGetString(items[i],"p");
      snap.pairs[i].bias=MseGetNumber(items[i],"bias",0.0);
      snap.pairs[i].bias_label=MseGetString(items[i],"lab");
      snap.pairs[i].direction=MseGetString(items[i],"dir");
      snap.pairs[i].p_base=MseGetNumber(items[i],"p0",0.0);
      snap.pairs[i].p_alt_a=MseGetNumber(items[i],"p1",0.0);
      snap.pairs[i].p_alt_b=MseGetNumber(items[i],"p2",0.0);
      snap.pairs[i].confidence=MseGetString(items[i],"conf");
      snap.pairs[i].expires=MseGetString(items[i],"exp");
      snap.pairs[i].rewritable=(MseGetNumber(items[i],"rw",0)>0.5);
      snap.pairs[i].minutes_to_red=(int)MseGetNumber(items[i],"redm",-1);
      snap.pairs[i].red_title=MseGetString(items[i],"redt");
      snap.pairs[i].no_trade=(MseGetNumber(items[i],"nt",0)>0.5);
      snap.pairs[i].no_trade_left=(int)MseGetNumber(items[i],"ntm",0);
      snap.pairs[i].no_trade_event=MseGetString(items[i],"nte");
      snap.pairs[i].action=MseGetString(items[i],"act");
     }

   //--- eventi
   string ev_body=MseGetArray(json,"events");
   count=MseSplitObjects(ev_body,items);
   ArrayResize(snap.events,count);
   for(int i=0;i<count;i++)
     {
      snap.events[i].ccy=MseGetString(items[i],"c");
      snap.events[i].title=MseGetString(items[i],"t");
      snap.events[i].impact=MseGetString(items[i],"im");
      snap.events[i].minutes=(int)MseGetNumber(items[i],"min",0);
      snap.events[i].ts=MseGetString(items[i],"ts");
     }

   if(ArraySize(snap.pairs)==0)
     {
      snap.error="Snapshot senza coppie: verificare la configurazione del backend";
      return(false);
     }

   snap.valid=true;
   return(true);
  }

//+------------------------------------------------------------------+
//| Sorgenti dati                                                     |
//+------------------------------------------------------------------+

//--- Modalita' (a): WebRequest verso l'endpoint locale
bool MseLoadFromWeb(const string url,const int timeout_ms,MacroSnapshot &snap)
  {
   char   post[];
   char   result[];
   string headers="Content-Type: application/json\r\n";
   string result_headers;

   ResetLastError();
   int status=WebRequest("GET",url,headers,timeout_ms,post,result,result_headers);
   if(status==-1)
     {
      int err=GetLastError();
      snap.valid=false;
      if(err==4014)
         snap.error="URL non autorizzato: aggiungerlo in Strumenti > Opzioni > Expert Advisors";
      else
         snap.error=StringFormat("WebRequest fallita (errore %d)",err);
      return(false);
     }
   if(status!=200)
     {
      snap.valid=false;
      snap.error=StringFormat("HTTP %d dall'endpoint",status);
      return(false);
     }
   string body=CharArrayToString(result,0,WHOLE_ARRAY,CP_UTF8);
   return(MseParseSnapshot(body,snap));
  }

//--- Modalita' (b): file JSON in MQL5\Files (prodotto da export_mt5_file.py)
bool MseLoadFromFile(const string filename,MacroSnapshot &snap)
  {
   ResetLastError();
   int handle=FileOpen(filename,FILE_READ|FILE_BIN|FILE_SHARE_READ|FILE_SHARE_WRITE);
   if(handle==INVALID_HANDLE)
     {
      snap.valid=false;
      snap.error=StringFormat("File %s non leggibile (errore %d)",filename,GetLastError());
      return(false);
     }
   ulong size=FileSize(handle);
   if(size==0)
     {
      FileClose(handle);
      snap.valid=false;
      snap.error="File snapshot vuoto";
      return(false);
     }
   uchar buffer[];
   ArrayResize(buffer,(int)size);
   uint read=FileReadArray(handle,buffer,0,(int)size);
   FileClose(handle);
   if(read==0)
     {
      snap.valid=false;
      snap.error="Lettura del file snapshot fallita";
      return(false);
     }
   string body=CharArrayToString(buffer,0,(int)read,CP_UTF8);
   return(MseParseSnapshot(body,snap));
  }

//+------------------------------------------------------------------+
//| Accessori                                                         |
//+------------------------------------------------------------------+

//--- Cerca la coppia corrispondente al simbolo del grafico
bool MseFindPair(const MacroSnapshot &snap,const string symbol,PairScenario &out)
  {
   string wanted=MseNormalizeSymbol(symbol);
   for(int i=0;i<ArraySize(snap.pairs);i++)
     {
      if(snap.pairs[i].pair==wanted)
        {
         out=snap.pairs[i];
         return(true);
        }
     }
   return(false);
  }

//--- Eta' dello snapshot in minuti (server time UTC)
double MseSnapshotAgeMinutes(const MacroSnapshot &snap)
  {
   if(snap.generated_time==0)
      return(99999.0);
   return((double)(TimeGMT()-snap.generated_time)/60.0);
  }

//--- Colore associato al bias
color MseBiasColor(const double bias)
  {
   if(bias>=40)
      return(clrLime);
   if(bias>=15)
      return(clrMediumSeaGreen);
   if(bias<=-40)
      return(clrRed);
   if(bias<=-15)
      return(clrTomato);
   return(clrSilver);
  }

//--- Colore associato alla confidenza
color MseConfidenceColor(const string confidence)
  {
   if(confidence=="ALTA")
      return(clrMediumSeaGreen);
   if(confidence=="MEDIA")
      return(clrGoldenrod);
   return(clrTomato);
  }
//+------------------------------------------------------------------+
