//+------------------------------------------------------------------+
//|                                         MacroScenarioEA.mq5       |
//|             Risk & Filter layer del Macro Scenario Engine         |
//+------------------------------------------------------------------+
//| QUESTO EA NON E' UN ENTRY SYSTEM. Non apre posizioni per conto     |
//| proprio: e' un livello di FILTRO e GESTIONE DEL RISCHIO attorno    |
//| agli eventi macro. Le entrate restano alla tua strategia.          |
//|                                                                    |
//| Cosa fa:                                                           |
//|   * blocca nuove aperture nelle finestre pre/post evento RED;      |
//|   * blocca i trade contro-bias oltre una soglia configurabile;     |
//|   * riduce il rischio per trade quando la confidenza e' BASSA;     |
//|   * avvisa quando lo scenario viene invalidato o rigenerato;       |
//|   * passa in modalita' protettiva se i dati sono obsoleti.         |
//|                                                                    |
//| MODALITA' DRY-RUN ATTIVA DI DEFAULT: l'EA logga le decisioni senza |
//| toccare le posizioni. Il passaggio a live e' un input esplicito.   |
//|                                                                    |
//| Come usarlo dalla tua strategia:                                   |
//|   1) leggi i buffer dell'indicatore con iCustom(); oppure          |
//|   2) chiama le funzioni globali MseTradeAllowed()/MseRiskFactor()  |
//|      compilando la tua strategia insieme a questo file; oppure     |
//|   3) lascialo in DRY-RUN come pannello di allerta e usa il log.    |
//+------------------------------------------------------------------+
#property copyright "Macro Scenario Engine"
#property link      "http://127.0.0.1:8000"
#property version   "1.00"
#property strict

#include <MacroScenarioTypes.mqh>
#include <Trade\Trade.mqh>

enum ENUM_MSE_SOURCE_EA
  {
   MSE_EA_FILE = 0,  // File JSON in MQL5\Files (consigliata)
   MSE_EA_WEB  = 1   // WebRequest verso endpoint locale
  };

//--- Sorgente dati
input ENUM_MSE_SOURCE_EA InpSource        = MSE_EA_FILE;                                  // Sorgente dati
input string  InpFileName                 = "macro_snapshot.json";                        // File in MQL5\Files
input string  InpUrl                      = "http://127.0.0.1:8000/api/v1/mt5/snapshot";  // Endpoint (da autorizzare)
input int     InpWebTimeoutMs             = 5000;                                         // Timeout WebRequest (ms)
input int     InpRefreshSeconds           = 60;                                           // Refresh dati (s)

//--- Sicurezza
input bool    InpDryRun                   = true;   // DRY-RUN: logga senza operare (default)
input int     InpStaleMinutes             = 30;     // Dati piu' vecchi di N min -> modalita' protettiva

//--- Filtri evento
input int     InpBlockMinutesBefore       = 30;     // Blocco: minuti PRIMA di un evento RED
input int     InpBlockMinutesAfter        = 15;     // Blocco: minuti DOPO un evento RED
input bool    InpUseBackendWindow         = true;   // Usa anche la finestra calcolata dal backend

//--- Filtri bias
input double  InpBlockAgainstBiasAbove    = 40.0;   // Blocca i trade contro-bias se |bias| supera questa soglia
input bool    InpBlockNeutralBias         = false;  // Blocca anche quando il bias e' neutro

//--- Rischio
input double  InpBaseRiskPercent          = 0.5;    // Rischio per trade di riferimento (%)
input double  InpLowConfidenceMultiplier  = 0.5;    // Moltiplicatore rischio se confidenza BASSA (0..1)
input double  InpMediumConfidenceMultiplier = 0.8;  // Moltiplicatore rischio se confidenza MEDIA (0..1)

//--- Gestione posizioni aperte (solo se DRY-RUN disattivato)
input bool    InpCloseBeforeRed           = false;  // Chiudi le posizioni prima di un evento RED
input int     InpCloseMinutesBefore       = 5;      // Quanti minuti prima chiuderle

//--- Notifiche
input bool    InpAlerts                   = true;   // Alert su invalidazione/rigenerazione scenario
input bool    InpPushNotifications        = false;  // Notifiche push (richiede MetaQuotes ID)
input ulong   InpMagicNumber              = 990011; // Magic number gestito da questo EA (0 = tutte)

//--- Stato
MacroSnapshot g_snap;
PairScenario  g_pair;
bool          g_has_pair       = false;
datetime      g_last_fetch     = 0;
string        g_last_signature = "";
string        g_last_expiry    = "";
bool          g_last_blocked   = false;
CTrade        g_trade;

//+------------------------------------------------------------------+
int OnInit()
  {
   g_trade.SetExpertMagicNumber(InpMagicNumber);

   PrintFormat("[MSE-EA] Avvio su %s | modalita' %s | sorgente %s",
               _Symbol,
               InpDryRun?"DRY-RUN (nessun ordine)":"LIVE",
               InpSource==MSE_EA_WEB?"WebRequest":"file JSON");
   if(!InpDryRun)
      Print("[MSE-EA] ATTENZIONE: modalita' LIVE attiva. L'EA puo' chiudere posizioni se configurato.");
   if(InpSource==MSE_EA_WEB)
      PrintFormat("[MSE-EA] Autorizzare %s in Strumenti > Opzioni > Expert Advisors",InpUrl);

   Fetch();
   EventSetTimer(5);
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
   Comment("");
  }

//+------------------------------------------------------------------+
void OnTimer()
  {
   if(TimeCurrent()-g_last_fetch>=InpRefreshSeconds)
      Fetch();

   MonitorScenario();
   ManageOpenPositions();
   ShowStatus();
  }

//+------------------------------------------------------------------+
void OnTick()
  {
   // L'EA non apre posizioni: qui si limita a tenere aggiornato lo stato.
   if(TimeCurrent()-g_last_fetch>=InpRefreshSeconds)
      Fetch();
  }

//+------------------------------------------------------------------+
//| Lettura dello snapshot                                            |
//+------------------------------------------------------------------+
void Fetch()
  {
   g_last_fetch=TimeCurrent();
   bool ok=(InpSource==MSE_EA_WEB)
           ? MseLoadFromWeb(InpUrl,InpWebTimeoutMs,g_snap)
           : MseLoadFromFile(InpFileName,g_snap);

   if(!ok)
     {
      g_has_pair=false;
      static string last_error="";
      if(last_error!=g_snap.error)
        {
         last_error=g_snap.error;
         PrintFormat("[MSE-EA] Dati non disponibili: %s -> modalita' protettiva",g_snap.error);
        }
      return;
     }
   g_has_pair=MseFindPair(g_snap,_Symbol,g_pair);
  }

//+------------------------------------------------------------------+
//| API pubblica per la tua strategia                                 |
//+------------------------------------------------------------------+

//--- true se e' consentito aprire una nuova posizione nella direzione data
//    (direction: +1 long, -1 short). `reason` spiega l'eventuale blocco.
bool MseTradeAllowed(const int direction,string &reason)
  {
   if(!g_snap.valid || !g_has_pair)
     {
      reason="dati macro non disponibili (modalita' protettiva)";
      return(false);
     }

   double age=MseSnapshotAgeMinutes(g_snap);
   if(age>InpStaleMinutes)
     {
      reason=StringFormat("dati obsoleti (%.0f min > %d)",age,InpStaleMinutes);
      return(false);
     }

   if(InpUseBackendWindow && g_pair.no_trade)
     {
      reason=StringFormat("finestra no-trade del backend attiva (%s, ancora %d min)",
                          g_pair.no_trade_event,g_pair.no_trade_left);
      return(false);
     }

   if(g_pair.minutes_to_red>=0)
     {
      if(g_pair.minutes_to_red<=InpBlockMinutesBefore)
        {
         reason=StringFormat("evento RED fra %d min (%s)",g_pair.minutes_to_red,g_pair.red_title);
         return(false);
        }
     }
   if(MinutesSinceLastRed()>=0 && MinutesSinceLastRed()<=InpBlockMinutesAfter)
     {
      reason="finestra post-evento RED";
      return(false);
     }

   if(InpBlockNeutralBias && g_pair.bias_label=="NEUTRAL")
     {
      reason="bias neutro: nessun vantaggio fondamentale";
      return(false);
     }

   if(direction>0 && g_pair.bias<-InpBlockAgainstBiasAbove)
     {
      reason=StringFormat("long contro-bias (bias %.1f < -%.1f)",g_pair.bias,InpBlockAgainstBiasAbove);
      return(false);
     }
   if(direction<0 && g_pair.bias>InpBlockAgainstBiasAbove)
     {
      reason=StringFormat("short contro-bias (bias %.1f > %.1f)",g_pair.bias,InpBlockAgainstBiasAbove);
      return(false);
     }

   reason="consentito";
   return(true);
  }

//--- Moltiplicatore di rischio 0..1 in base alla confidenza dello scenario
double MseRiskFactor()
  {
   if(!g_snap.valid || !g_has_pair)
      return(0.0);
   if(MseSnapshotAgeMinutes(g_snap)>InpStaleMinutes)
      return(0.0);
   if(g_pair.confidence=="BASSA")
      return(MathMax(0.0,MathMin(1.0,InpLowConfidenceMultiplier)));
   if(g_pair.confidence=="MEDIA")
      return(MathMax(0.0,MathMin(1.0,InpMediumConfidenceMultiplier)));
   return(1.0);
  }

//--- Rischio percentuale suggerito per il prossimo trade
double MseSuggestedRiskPercent()
  {
   return(InpBaseRiskPercent*MseRiskFactor());
  }

//+------------------------------------------------------------------+
//| Minuti trascorsi dall'ultimo evento RED (-1 se nessuno noto)      |
//+------------------------------------------------------------------+
int MinutesSinceLastRed()
  {
   int best=-1;
   for(int i=0;i<ArraySize(g_snap.events);i++)
     {
      if(g_snap.events[i].impact!="RED")
         continue;
      if(g_snap.events[i].minutes<0)
        {
         int elapsed=-g_snap.events[i].minutes;
         if(best<0 || elapsed<best)
            best=elapsed;
        }
     }
   return(best);
  }

//+------------------------------------------------------------------+
//| Rilevamento invalidazione / rigenerazione dello scenario          |
//+------------------------------------------------------------------+
void MonitorScenario()
  {
   if(!g_snap.valid || !g_has_pair)
      return;

   string signature=StringFormat("%s|%.1f|%s|%.1f",
                                 g_pair.direction,g_pair.bias,g_pair.confidence,g_pair.p_base);

   if(g_last_signature=="")
     {
      g_last_signature=signature;
      g_last_expiry=g_pair.expires;
      return;
     }

   if(signature!=g_last_signature)
     {
      string message=StringFormat("[MSE] %s scenario rigenerato: %s (bias %.1f, conf. %s, %.1f%%)",
                                  MseNormalizeSymbol(_Symbol),g_pair.direction,
                                  g_pair.bias,g_pair.confidence,g_pair.p_base);
      Notify(message);
      g_last_signature=signature;
     }

   if(g_pair.expires!=g_last_expiry)
     {
      g_last_expiry=g_pair.expires;
     }

   datetime expiry=MseParseIso(g_pair.expires);
   if(expiry>0 && TimeGMT()>expiry)
      Notify(StringFormat("[MSE] %s scenario SCADUTO (%s): attendere la rigenerazione",
                          MseNormalizeSymbol(_Symbol),StringSubstr(g_pair.expires,0,16)));

   if(g_pair.rewritable)
      {
       static datetime last_warn=0;
       if(TimeCurrent()-last_warn>1800)
         {
          last_warn=TimeCurrent();
          Notify(StringFormat("[MSE] %s scenario RISCRIVIBILE: evento RED fra %d min",
                              MseNormalizeSymbol(_Symbol),g_pair.minutes_to_red));
         }
      }
  }

//+------------------------------------------------------------------+
//| Gestione delle posizioni aperte (solo in modalita' LIVE)          |
//+------------------------------------------------------------------+
void ManageOpenPositions()
  {
   if(!InpCloseBeforeRed)
      return;
   if(!g_snap.valid || !g_has_pair)
      return;
   if(g_pair.minutes_to_red<0 || g_pair.minutes_to_red>InpCloseMinutesBefore)
      return;

   for(int i=PositionsTotal()-1;i>=0;i--)
     {
      ulong ticket=PositionGetTicket(i);
      if(ticket==0)
         continue;
      if(PositionGetString(POSITION_SYMBOL)!=_Symbol)
         continue;
      if(InpMagicNumber!=0 && (ulong)PositionGetInteger(POSITION_MAGIC)!=InpMagicNumber)
         continue;

      string message=StringFormat("[MSE] Chiusura pre-evento posizione #%I64u su %s (RED fra %d min: %s)",
                                  ticket,_Symbol,g_pair.minutes_to_red,g_pair.red_title);
      if(InpDryRun)
        {
         Print(message," [DRY-RUN: nessun ordine inviato]");
         continue;
        }
      if(g_trade.PositionClose(ticket))
         Notify(message);
      else
         PrintFormat("[MSE] Chiusura #%I64u fallita: %d %s",
                     ticket,g_trade.ResultRetcode(),g_trade.ResultRetcodeDescription());
     }
  }

//+------------------------------------------------------------------+
//| Pannello di stato + log delle decisioni                           |
//+------------------------------------------------------------------+
void ShowStatus()
  {
   string reason_long="";
   string reason_short="";
   bool allow_long=MseTradeAllowed(1,reason_long);
   bool allow_short=MseTradeAllowed(-1,reason_short);
   bool blocked=(!allow_long && !allow_short);

   if(blocked!=g_last_blocked)
     {
      g_last_blocked=blocked;
      PrintFormat("[MSE-EA] %s: long=%s (%s) | short=%s (%s) | rischio suggerito %.2f%%",
                  _Symbol,
                  allow_long?"OK":"BLOCCATO",reason_long,
                  allow_short?"OK":"BLOCCATO",reason_short,
                  MseSuggestedRiskPercent());
     }

   string status;
   if(!g_snap.valid || !g_has_pair)
      status=StringFormat("MACRO SCENARIO EA  |  %s\nDATI NON DISPONIBILI: %s\nMODALITA' PROTETTIVA: nessuna nuova apertura",
                          InpDryRun?"DRY-RUN":"LIVE",g_snap.error);
   else
      status=StringFormat(
                "MACRO SCENARIO EA  |  %s  |  dati %.0f min fa\n"
                "%s  bias %+.1f (%s)  |  scenario %s %.1f%%  |  confidenza %s%s\n"
                "prossimo RED: %s%s\n"
                "LONG  : %s (%s)\n"
                "SHORT : %s (%s)\n"
                "rischio suggerito: %.2f%% (base %.2f%% x %.2f)\n"
                "Stime di modello - non sono consulenza finanziaria",
                InpDryRun?"DRY-RUN":"LIVE",MseSnapshotAgeMinutes(g_snap),
                MseNormalizeSymbol(_Symbol),g_pair.bias,g_pair.bias_label,
                g_pair.direction,g_pair.p_base,g_pair.confidence,
                g_pair.rewritable?" [RISCRIVIBILE]":"",
                g_pair.minutes_to_red>=0?StringFormat("fra %d min - %s",g_pair.minutes_to_red,g_pair.red_title):"nessuno",
                g_pair.no_trade?StringFormat("\n### NO-TRADE ATTIVO ### %s (ancora %d min)",g_pair.no_trade_event,g_pair.no_trade_left):"",
                allow_long?"consentito":"BLOCCATO",reason_long,
                allow_short?"consentito":"BLOCCATO",reason_short,
                MseSuggestedRiskPercent(),InpBaseRiskPercent,MseRiskFactor());

   Comment(status);
  }

//+------------------------------------------------------------------+
void Notify(const string message)
  {
   Print(message);
   if(InpAlerts)
      Alert(message);
   if(InpPushNotifications)
      SendNotification(message);
  }
//+------------------------------------------------------------------+
