//+------------------------------------------------------------------+
//|                                      MacroScenarioBridge.mq5      |
//|            Pannello grafico + buffer dati del Macro Scenario Engine|
//+------------------------------------------------------------------+
//| Legge lo snapshot prodotto dal backend (WebRequest o file JSON) e: |
//|   * disegna un pannello con bias, probabilita', confidenza,        |
//|     countdown al prossimo evento RED e badge NO-TRADE;             |
//|   * espone 4 buffer riutilizzabili da altri EA/indicatori:         |
//|       0 = bias di coppia (-100..+100)                              |
//|       1 = probabilita' dello scenario base (%)                     |
//|       2 = minuti al prossimo evento RED (-1 se nessuno)            |
//|       3 = flag no-trade (0/1)                                      |
//|                                                                    |
//| Nota: il pannello usa oggetti OBJ_ (persistenti), non Comment().   |
//+------------------------------------------------------------------+
#property copyright "Macro Scenario Engine"
#property link      "http://127.0.0.1:8000"
#property version   "1.00"
#property strict
#property indicator_chart_window
#property indicator_buffers 4
#property indicator_plots   0

#include <MacroScenarioTypes.mqh>

//--- Modalita' di alimentazione dati
enum ENUM_MSE_SOURCE
  {
   MSE_SOURCE_FILE = 0,  // File JSON in MQL5\Files (consigliata)
   MSE_SOURCE_WEB  = 1   // WebRequest verso endpoint locale
  };

//--- Input
input ENUM_MSE_SOURCE InpSource            = MSE_SOURCE_FILE;                        // Sorgente dati
input string          InpFileName          = "macro_snapshot.json";                  // File in MQL5\Files
input string          InpUrl               = "http://127.0.0.1:8000/api/v1/mt5/snapshot"; // Endpoint (da autorizzare in MT5)
input int             InpWebTimeoutMs      = 5000;                                   // Timeout WebRequest (ms)
input int             InpRefreshSeconds    = 60;                                     // Refresh normale (s)
input int             InpRefreshAroundRed  = 300;                                    // Refresh forzato attorno a eventi RED (s)
input int             InpRedWindowMinutes  = 60;                                     // "Attorno a RED" = entro N minuti
input int             InpStaleMinutes      = 30;                                     // Dati piu' vecchi di N min = allarme
input int             InpPanelX            = 12;                                     // Pannello: offset X
input int             InpPanelY            = 22;                                     // Pannello: offset Y
input int             InpFontSize          = 9;                                      // Dimensione carattere
input bool            InpShowEvents        = true;                                   // Mostra i prossimi eventi
input int             InpMaxEvents         = 3;                                      // Quanti eventi mostrare

//--- Buffer
double BufBias[];
double BufProbBase[];
double BufMinutesToRed[];
double BufNoTrade[];

//--- Stato
const string   PREFIX = "MSE_";
MacroSnapshot  g_snap;
PairScenario   g_pair;
bool           g_has_pair = false;
datetime       g_last_fetch = 0;
string         g_last_error = "";

//+------------------------------------------------------------------+
int OnInit()
  {
   SetIndexBuffer(0,BufBias,INDICATOR_DATA);
   SetIndexBuffer(1,BufProbBase,INDICATOR_DATA);
   SetIndexBuffer(2,BufMinutesToRed,INDICATOR_DATA);
   SetIndexBuffer(3,BufNoTrade,INDICATOR_DATA);

   ArraySetAsSeries(BufBias,true);
   ArraySetAsSeries(BufProbBase,true);
   ArraySetAsSeries(BufMinutesToRed,true);
   ArraySetAsSeries(BufNoTrade,true);

   IndicatorSetString(INDICATOR_SHORTNAME,"Macro Scenario Bridge");
   IndicatorSetInteger(INDICATOR_DIGITS,1);

   if(InpSource==MSE_SOURCE_WEB)
      PrintFormat("[MSE] Modalita' WebRequest: autorizzare %s in Strumenti > Opzioni > Expert Advisors",InpUrl);

   FetchSnapshot();
   EventSetTimer(1);
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
   ObjectsDeleteAll(0,PREFIX);
   ChartRedraw();
  }

//+------------------------------------------------------------------+
int OnCalculate(const int rates_total,
                const int prev_calculated,
                const datetime &time[],
                const double &open[],
                const double &high[],
                const double &low[],
                const double &close[],
                const long &tick_volume[],
                const long &volume[],
                const int &spread[])
  {
   FillBuffers(rates_total);
   return(rates_total);
  }

//+------------------------------------------------------------------+
//| Timer: refresh dei dati e ridisegno del pannello                  |
//+------------------------------------------------------------------+
void OnTimer()
  {
   int interval=RefreshInterval();
   if(TimeCurrent()-g_last_fetch>=interval)
      FetchSnapshot();

   DrawPanel();
   ChartRedraw();
  }

//+------------------------------------------------------------------+
//| Refresh piu' frequente attorno agli eventi ad alto impatto        |
//+------------------------------------------------------------------+
int RefreshInterval()
  {
   if(g_has_pair && g_pair.minutes_to_red>=0 && g_pair.minutes_to_red<=InpRedWindowMinutes)
      return(MathMin(InpRefreshSeconds,InpRefreshAroundRed));
   return(InpRefreshSeconds);
  }

//+------------------------------------------------------------------+
//| Lettura dello snapshot                                            |
//+------------------------------------------------------------------+
void FetchSnapshot()
  {
   g_last_fetch=TimeCurrent();
   bool ok=false;

   if(InpSource==MSE_SOURCE_WEB)
      ok=MseLoadFromWeb(InpUrl,InpWebTimeoutMs,g_snap);
   else
      ok=MseLoadFromFile(InpFileName,g_snap);

   if(!ok)
     {
      if(g_last_error!=g_snap.error)
        {
         g_last_error=g_snap.error;
         PrintFormat("[MSE] %s",g_snap.error);
        }
      g_has_pair=false;
      return;
     }

   g_last_error="";
   g_has_pair=MseFindPair(g_snap,_Symbol,g_pair);
   if(!g_has_pair)
      PrintFormat("[MSE] La coppia %s non e' configurata nel backend (config/operator.yaml)",
                  MseNormalizeSymbol(_Symbol));

   FillBuffers(Bars(_Symbol,_Period));
  }

//+------------------------------------------------------------------+
//| Riempimento dei buffer esposti (valore corrente sulle ultime barre)|
//+------------------------------------------------------------------+
void FillBuffers(const int rates_total)
  {
   double bias   = g_has_pair ? g_pair.bias : 0.0;
   double prob   = g_has_pair ? g_pair.p_base : 0.0;
   double red    = g_has_pair ? (double)g_pair.minutes_to_red : -1.0;
   double notrade= (g_has_pair && g_pair.no_trade) ? 1.0 : 0.0;

   // Dati troppo vecchi: modalita' protettiva anche per i consumatori dei buffer.
   if(g_has_pair && MseSnapshotAgeMinutes(g_snap)>InpStaleMinutes)
     {
      bias=0.0;
      prob=0.0;
      notrade=1.0;
     }

   // I buffer vengono allocati dal terminale: prima di OnCalculate possono
   // essere ancora vuoti, quindi si scrive solo entro la dimensione reale.
   int available=ArraySize(BufBias);
   int limit=MathMin(MathMin(rates_total,available),200);
   for(int i=0;i<limit;i++)
     {
      BufBias[i]=bias;
      BufProbBase[i]=prob;
      BufMinutesToRed[i]=red;
      BufNoTrade[i]=notrade;
     }
  }

//+------------------------------------------------------------------+
//| Pannello grafico                                                  |
//+------------------------------------------------------------------+
void DrawPanel()
  {
   int y=InpPanelY;
   int line_height=InpFontSize+6;
   int row=0;

   DrawBackground();

   if(!g_snap.valid)
     {
      Label("err",InpPanelX+8,y+row*line_height,"MACRO SCENARIO ENGINE - DATI NON DISPONIBILI",clrTomato,true);
      row++;
      Label("err2",InpPanelX+8,y+row*line_height,g_snap.error,clrSilver);
      HideUnusedEventRows(0);
      return;
     }

   double age=MseSnapshotAgeMinutes(g_snap);
   bool stale=(age>InpStaleMinutes);

   Label("title",InpPanelX+8,y+row*line_height,
         StringFormat("MACRO SCENARIO ENGINE  |  %s  |  orizzonte %s",
                      MseNormalizeSymbol(_Symbol),g_snap.horizon),
         clrWhite,true);
   row++;

   Label("age",InpPanelX+8,y+row*line_height,
         StringFormat("dati aggiornati %.0f min fa%s",age,stale?"  <-- OBSOLETI":""),
         stale?clrTomato:clrSilver);
   row++;

   if(!g_has_pair)
     {
      Label("nopair",InpPanelX+8,y+row*line_height,
            "Coppia non configurata nel backend",clrGoldenrod);
      HideUnusedEventRows(0);
      return;
     }

   //--- bias
   string arrow=(g_pair.bias>0?"^":(g_pair.bias<0?"v":"-"));
   Label("bias",InpPanelX+8,y+row*line_height,
         StringFormat("BIAS %s %+.1f  (%s)",arrow,g_pair.bias,g_pair.bias_label),
         MseBiasColor(g_pair.bias),true);
   row++;

   //--- scenari con barra orizzontale
   DrawProbability("p0",row,y,line_height,"BASE  "+g_pair.direction,g_pair.p_base,MseBiasColor(g_pair.bias));
   row++;
   DrawProbability("p1",row,y,line_height,"ALT A hawkish/risk-on",g_pair.p_alt_a,clrMediumSeaGreen);
   row++;
   DrawProbability("p2",row,y,line_height,"ALT B dovish/risk-off",g_pair.p_alt_b,clrTomato);
   row++;

   //--- confidenza e scadenza
   Label("conf",InpPanelX+8,y+row*line_height,
         StringFormat("CONFIDENZA %s%s  |  scade %s",
                      g_pair.confidence,
                      g_pair.rewritable?"  [RISCRIVIBILE]":"",
                      StringSubstr(g_pair.expires,0,16)),
         MseConfidenceColor(g_pair.confidence));
   row++;

   //--- countdown evento RED
   if(g_pair.minutes_to_red>=0)
     {
      color red_color=(g_pair.minutes_to_red<=30)?clrRed:(g_pair.minutes_to_red<=240?clrGoldenrod:clrSilver);
      Label("red",InpPanelX+8,y+row*line_height,
            StringFormat("PROSSIMO RED fra %s  |  %s",
                         FormatMinutes(g_pair.minutes_to_red),g_pair.red_title),
            red_color);
     }
   else
      Label("red",InpPanelX+8,y+row*line_height,"Nessun evento RED in calendario",clrSilver);
   row++;

   //--- badge no-trade / suggerimento
   if(g_pair.no_trade)
      Label("nt",InpPanelX+8,y+row*line_height,
            StringFormat("### NO-TRADE ###  %s  (ancora %d min)",
                         g_pair.no_trade_event,g_pair.no_trade_left),
            clrRed,true);
   else
      Label("nt",InpPanelX+8,y+row*line_height,
            StringFormat("Suggerimento non vincolante: %s",g_pair.action),clrSilver);
   row++;

   //--- eventi imminenti
   if(InpShowEvents)
     {
      int shown=0;
      for(int i=0;i<ArraySize(g_snap.events) && shown<InpMaxEvents;i++)
        {
         color c=(g_snap.events[i].impact=="RED")?clrTomato:clrGoldenrod;
         Label("ev"+IntegerToString(shown),InpPanelX+8,y+row*line_height,
               StringFormat("%-6s %s  %s  fra %s",
                            g_snap.events[i].ccy,
                            g_snap.events[i].impact,
                            g_snap.events[i].title,
                            FormatMinutes(g_snap.events[i].minutes)),
               c);
         row++;
         shown++;
        }
      HideUnusedEventRows(shown);
     }

   Label("disc",InpPanelX+8,y+row*line_height,
         "Stime di modello - non sono consulenza finanziaria",clrDimGray);
  }

//+------------------------------------------------------------------+
void DrawProbability(const string id,const int row,const int y,const int line_height,
                     const string caption,const double probability,const color bar_color)
  {
   // Barra orizzontale in caratteri: un blocco ogni 5 punti percentuali.
   string bar="";
   int blocks=(int)MathRound(probability/5.0);
   for(int i=0;i<blocks;i++)
      bar+="|";
   Label(id,InpPanelX+8,y+row*line_height,
         StringFormat("%-24s %5.1f%%  %s",caption,probability,bar),bar_color);
  }

//+------------------------------------------------------------------+
string FormatMinutes(const int minutes)
  {
   if(minutes<0)
      return("n/d");
   if(minutes<60)
      return(StringFormat("%d min",minutes));
   if(minutes<1440)
      return(StringFormat("%dh%02d",minutes/60,minutes%60));
   return(StringFormat("%dg %dh",minutes/1440,(minutes%1440)/60));
  }

//+------------------------------------------------------------------+
void DrawBackground()
  {
   string name=PREFIX+"bg";
   if(ObjectFind(0,name)<0)
     {
      ObjectCreate(0,name,OBJ_RECTANGLE_LABEL,0,0,0);
      ObjectSetInteger(0,name,OBJPROP_CORNER,CORNER_LEFT_UPPER);
      ObjectSetInteger(0,name,OBJPROP_BACK,false);
      ObjectSetInteger(0,name,OBJPROP_SELECTABLE,false);
      ObjectSetInteger(0,name,OBJPROP_HIDDEN,true);
      ObjectSetInteger(0,name,OBJPROP_BORDER_TYPE,BORDER_FLAT);
      ObjectSetInteger(0,name,OBJPROP_COLOR,clrDimGray);
      ObjectSetInteger(0,name,OBJPROP_BGCOLOR,C'12,16,22');
     }
   ObjectSetInteger(0,name,OBJPROP_XDISTANCE,InpPanelX);
   ObjectSetInteger(0,name,OBJPROP_YDISTANCE,InpPanelY-6);
   ObjectSetInteger(0,name,OBJPROP_XSIZE,430);
   ObjectSetInteger(0,name,OBJPROP_YSIZE,(InpFontSize+6)*(10+InpMaxEvents)+12);
  }

//+------------------------------------------------------------------+
void Label(const string id,const int x,const int y,const string text,
           const color clr,const bool bold=false)
  {
   string name=PREFIX+id;
   if(ObjectFind(0,name)<0)
     {
      ObjectCreate(0,name,OBJ_LABEL,0,0,0);
      ObjectSetInteger(0,name,OBJPROP_CORNER,CORNER_LEFT_UPPER);
      ObjectSetInteger(0,name,OBJPROP_SELECTABLE,false);
      ObjectSetInteger(0,name,OBJPROP_HIDDEN,true);
     }
   ObjectSetInteger(0,name,OBJPROP_XDISTANCE,x);
   ObjectSetInteger(0,name,OBJPROP_YDISTANCE,y);
   ObjectSetInteger(0,name,OBJPROP_COLOR,clr);
   ObjectSetInteger(0,name,OBJPROP_FONTSIZE,InpFontSize);
   ObjectSetString(0,name,OBJPROP_FONT,bold?"Consolas Bold":"Consolas");
   ObjectSetString(0,name,OBJPROP_TEXT,text);
  }

//+------------------------------------------------------------------+
//| Svuota le righe evento non usate in questo ciclo di disegno       |
//+------------------------------------------------------------------+
void HideUnusedEventRows(const int shown)
  {
   for(int i=shown;i<InpMaxEvents;i++)
     {
      string name=PREFIX+"ev"+IntegerToString(i);
      if(ObjectFind(0,name)>=0)
         ObjectSetString(0,name,OBJPROP_TEXT,"");
     }
  }
//+------------------------------------------------------------------+
