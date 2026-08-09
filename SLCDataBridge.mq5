//+------------------------------------------------------------------+
//|  SLCDataBridge.mq5                                               |
//|  Pushes live account, position, and trade data to the local      |
//|  Python dashboard server via HTTP POST on a timer.               |
//|  Also polls for SL management commands from the news-agent and   |
//|  executes them (trail SL or move to break-even).                 |
//|                                                                  |
//|  Setup (one-time):                                               |
//|    1. Open MT5 → Tools → Options → Expert Advisors               |
//|    2. Tick "Allow WebRequest for listed URL"                     |
//|    3. Add:  http://<ServerHost>:<ServerPort>                     |
//|       e.g. http://192.168.68.104:8766                            |
//|       (one entry covers every endpoint this EA uses)             |
//|    4. Attach this EA to any chart (e.g. EURUSD M1)               |
//|                                                                  |
//|  Python processes to run:                                        |
//|    Terminal 1:  cd trading-bot && python server.py               |
//|    Terminal 2:  cd trading-bot && python news_agent.py           |
//+------------------------------------------------------------------+
#property copyright "SLC Data Bridge"
#property version   "2.30"
// Version is 2.30 across the #property, the startup Print, and the JSON feed payload.

#include <Trade\Trade.mqh>

//--- Server connection ----------------------------------------------
// SINGLE source of truth for where the Python server lives. Every
// endpoint URL (/api/mt5_feed, /api/mt5_bars, /api/pairs, /api/commands/*)
// is derived from these two values at startup, so changing servers means
// editing one host instead of five URLs. If MT5 runs on the SAME Mac as
// the server, use "127.0.0.1".
input string   ServerHost         = "192.168.68.104";  // Server IP/hostname (use 127.0.0.1 if same machine)
input int      ServerPort         = 8766;              // Server port (config.yaml → server.port)

//--- Data push inputs
input int      PushIntervalSec    = 5;    // How often to push account/price data (seconds)
input int      ClosedTradesDays   = 30;   // Days of closed trades to include

//--- Dashboard-driven symbol list ----------------------------------
// When UseDashboardPairs = true, the EA fetches the pairs you've enabled
// in the dashboard Pairs Manager (GET /api/pairs) and pushes BOTH prices
// and chart bars for exactly those symbols. Symbols your broker doesn't
// offer are validated and skipped automatically. The static lists below
// are used only as a fallback (server unreachable, or list empty).
input bool     UseDashboardPairs  = true;  // Watch the dashboard's enabled pairs automatically
input int      PairsRefreshSec    = 30;    // How often to re-fetch the enabled-pairs list (seconds)

//--- Broker symbol aliases -----------------------------------------
// The dashboard catalog uses generic names (USOIL, US30, GER40, BTCUSD…)
// but your broker may name them differently (USOUSD, DJ30, DE40…). Map
// dashboard name → broker symbol here, comma-separated as FROM=TO.
// Matching is case-insensitive. Names not listed are used as-is.
input string   SymbolAliases      = "USOIL=USOUSD";  // e.g. "USOIL=USOUSD,US30=DJ30,GER40=DE40"

//--- Live price watchlist (FALLBACK when UseDashboardPairs off / unreachable)
input string   SymbolsToWatch     = "EURUSD,GBPUSD,USDJPY,AUDUSD,USDCAD,XAUUSD,USOUSD";  // Comma-separated

//--- OHLCV bar push settings
input string   BarSymbols         = "EURUSD,GBPUSD,USDJPY,XAUUSD,USOUSD";  // FALLBACK bar symbols (when UseDashboardPairs off)
input int      BarPushIntervalSec = 60;   // How often to push bar data (seconds, match engine poll_seconds)
input int      BarsToSend         = 300;  // Bars per symbol per timeframe (covers initial_history_bars + lookback)

//--- Command polling inputs
input int      PollCommandsSec    = 10;   // How often to poll for commands (seconds)
input bool     EnableCommands     = true; // Set false to disable all SL command execution

//--- Trade execution safety (v2.30) ---------------------------------
// AllowTradeExecution is the MASTER switch for opening/closing positions
// from server commands (live mode). It is OFF by default: with it off the
// EA still pushes data and manages SL, but refuses open_trade/close_trade
// commands (they are acked with a log line so they don't loop forever).
// MaxLotsPerTrade is a hard broker-side cap regardless of what the
// server requests.
input bool     AllowTradeExecution = false;  // MASTER SWITCH: allow open/close from server (live mode)
input double   MaxLotsPerTrade     = 1.0;    // Hard cap on lots per order
input int      MaxOpenPositions    = 6;      // Refuse new opens beyond this many positions

//--- Logging
input bool     VerboseLog         = false; // Log every push/poll to Experts tab

//--- State
int    g_pushCount    = 0;
int    g_barPushCount = 0;
int    g_pollCount    = 0;
int    g_pairsCount   = 0;       // timer counter for enabled-pairs refresh
string g_dynamicSymbols[];       // DASHBOARD names (the identity used everywhere downstream)
string g_dynBroker[];            // matching BROKER symbols (used only for MT5 API calls)
int    g_dynCount     = 0;       // number of valid dynamic symbols (0 = use fallback)
//--- Endpoint URLs, derived from ServerHost/ServerPort in OnInit
string g_feedURL;                // /api/mt5_feed
string g_barsURL;                // /api/mt5_bars
string g_commandsURL;            // /api/commands/next
string g_pairsURL;               // /api/pairs
string g_ackBase;                // /api/commands/ack/   (+ cmdId)
//--- Symbol alias table (dashboard name → broker symbol)
string g_aliasFrom[];
string g_aliasTo[];
int    g_aliasCount   = 0;
//--- Cached index of THIS broker's symbols (normalized) for auto-resolution
string g_bsymName[];          // exact broker symbol name
string g_bsymNorm[];          // normalized (uppercase, alphanumerics only)
int    g_bsymCount    = 0;
bool   g_bsymBuilt    = false;
CTrade g_trade;
ulong  g_lastSpanMsc = 0;             // start (ms) of the current tick-scan window

//+------------------------------------------------------------------+
int OnInit()
  {
   EventSetTimer(1);   // 1-second tick for flexible interval counting
   g_lastSpanMsc = (ulong)TimeCurrent() * 1000;   // refined on every push
   g_trade.SetExpertMagicNumber(0);
   g_trade.SetDeviationInPoints(30);

   // Derive every endpoint from the single ServerHost/ServerPort pair.
   string base   = "http://" + ServerHost + ":" + IntegerToString(ServerPort);
   g_feedURL     = base + "/api/mt5_feed";
   g_barsURL     = base + "/api/mt5_bars";
   g_commandsURL = base + "/api/commands/next";
   g_pairsURL    = base + "/api/pairs";
   g_ackBase     = base + "/api/commands/ack/";

   // Parse the dashboard→broker symbol alias map, then index this broker's
   // symbols so dashboard names auto-resolve to the broker's actual names.
   ParseAliases();
   BuildBrokerSymbolIndex();

   Print("SLCDataBridge v2.30: started.");
   Print("  Server : ", base);
   Print("  Push   : ", g_feedURL, "  every ", PushIntervalSec, "s");
   Print("  Bars   : ", g_barsURL, "  every ", BarPushIntervalSec, "s");
   if(EnableCommands)
      Print("  Commands: ", g_commandsURL, "  every ", PollCommandsSec, "s");
   else
      Print("  Commands: DISABLED (EnableCommands=false)");
   if(UseDashboardPairs)
      Print("  Pairs  : ", g_pairsURL, "  every ", PairsRefreshSec, "s (dashboard-driven)");
   else
      Print("  Pairs  : static inputs (UseDashboardPairs=false)");
   Print("  Aliases: ", g_aliasCount, " symbol mapping(s) loaded.");

   // Fetch the dashboard's enabled pairs before the first push so prices
   // and bars cover exactly what's toggled on.
   FetchEnabledPairs();

   // Push immediately on attach so the engine has data without waiting
   PushData();
   PushBars();

   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   Print("SLCDataBridge: stopped.");
  }

void OnTimer()
  {
   g_pushCount++;
   g_barPushCount++;
   g_pollCount++;
   g_pairsCount++;

   if(UseDashboardPairs && g_pairsCount >= PairsRefreshSec)
     {
      g_pairsCount = 0;
      FetchEnabledPairs();
     }

   if(g_pushCount >= PushIntervalSec)
     {
      g_pushCount = 0;
      PushData();
     }

   if(g_barPushCount >= BarPushIntervalSec)
     {
      g_barPushCount = 0;
      PushBars();
     }

   if(EnableCommands && g_pollCount >= PollCommandsSec)
     {
      g_pollCount = 0;
      PollAndExecuteCommand();
     }
  }

//+------------------------------------------------------------------+
//| Build the effective watch list as parallel arrays:                |
//|   dash[]   = dashboard names (the identity emitted in JSON)        |
//|   broker[] = matching broker symbols (used for MT5 API calls)      |
//| Uses the dashboard-resolved set when available, else splits the    |
//| static fallback CSV and resolves each entry to a broker symbol.    |
//+------------------------------------------------------------------+
int BuildWatchPairs(const string fallbackCsv, string &dash[], string &broker[])
  {
   if(UseDashboardPairs && g_dynCount > 0)
     {
      ArrayResize(dash, g_dynCount);
      ArrayResize(broker, g_dynCount);
      for(int i = 0; i < g_dynCount; i++)
        {
         dash[i]   = g_dynamicSymbols[i];
         broker[i] = g_dynBroker[i];
        }
      return g_dynCount;
     }
   int n = StringSplit(fallbackCsv, ',', dash);
   ArrayResize(broker, n);
   for(int i = 0; i < n; i++)
     {
      StringTrimLeft(dash[i]); StringTrimRight(dash[i]);
      broker[i] = ResolveSymbol(dash[i]);
     }
   return n;
  }

//+------------------------------------------------------------------+
//| Parse the SymbolAliases input ("FROM=TO,FROM=TO,…") into the      |
//| g_aliasFrom[] / g_aliasTo[] lookup table. Called once in OnInit.  |
//+------------------------------------------------------------------+
void ParseAliases()
  {
   g_aliasCount = 0;
   ArrayResize(g_aliasFrom, 0);
   ArrayResize(g_aliasTo, 0);

   string entries[];
   int n = StringSplit(SymbolAliases, ',', entries);
   for(int i = 0; i < n; i++)
     {
      string e = entries[i];
      StringTrimLeft(e);
      StringTrimRight(e);
      if(StringLen(e) == 0) continue;
      int eq = StringFind(e, "=");
      if(eq <= 0) continue;
      string from = StringSubstr(e, 0, eq);
      string to   = StringSubstr(e, eq + 1);
      StringTrimLeft(from); StringTrimRight(from);
      StringTrimLeft(to);   StringTrimRight(to);
      if(StringLen(from) == 0 || StringLen(to) == 0) continue;
      ArrayResize(g_aliasFrom, g_aliasCount + 1);
      ArrayResize(g_aliasTo,   g_aliasCount + 1);
      g_aliasFrom[g_aliasCount] = from;
      g_aliasTo[g_aliasCount]   = to;
      g_aliasCount++;
     }
  }

//+------------------------------------------------------------------+
//| Normalize a symbol name: uppercase, keep only A-Z/0-9. This lets   |
//| us match "US30" to "US30.cash", "#US30", "BTCUSD" to "BTCUSD.m".   |
//+------------------------------------------------------------------+
string NormalizeSym(string s)
  {
   StringToUpper(s);
   string out = "";
   int n = StringLen(s);
   for(int i = 0; i < n; i++)
     {
      ushort ch = StringGetCharacter(s, i);
      if((ch >= 'A' && ch <= 'Z') || (ch >= '0' && ch <= '9'))
         out += ShortToString(ch);
     }
   return out;
  }

//+------------------------------------------------------------------+
//| Build a cached, normalized index of every symbol THIS broker has. |
//+------------------------------------------------------------------+
void BuildBrokerSymbolIndex()
  {
   int total = SymbolsTotal(false);          // all symbols, not just Market Watch
   ArrayResize(g_bsymName, total);
   ArrayResize(g_bsymNorm, total);
   for(int i = 0; i < total; i++)
     {
      string nm = SymbolName(i, false);
      g_bsymName[i] = nm;
      g_bsymNorm[i] = NormalizeSym(nm);
     }
   g_bsymCount = total;
   g_bsymBuilt = true;
   if(VerboseLog) Print("SLCDataBridge: indexed ", total, " broker symbols for auto-resolution.");
  }

//+------------------------------------------------------------------+
//| Common cross-broker synonyms for non-FX instruments. Returns a    |
//| comma-separated candidate list for a dashboard name (or "").      |
//+------------------------------------------------------------------+
string SynonymsCSV(string d)
  {
   StringToUpper(d);
   if(d == "US30")   return "DJ30,WS30,DJI30,DJIA,USA30,US30Cash,DJ30Cash,WallStreet30";
   if(d == "US500")  return "SPX500,SP500,SPX,USA500,US500Cash,SP500m";
   if(d == "NAS100") return "USTEC,US100,NDX100,USTECH,USATEC,NAS100Cash,USTECm";
   if(d == "GER40")  return "DE40,DAX40,GER30,DE30,GERMANY40,DAX40Cash,DE40Cash";
   if(d == "UK100")  return "FTSE100,GB100,UK100Cash,FTSE";
   if(d == "JPN225") return "JP225,JAPAN225,NIKKEI,JPN225Cash,JP225Cash";
   if(d == "AUS200") return "AU200,AUSTRALIA200,AUS200Cash,ASX200";
   if(d == "USOIL")  return "WTI,USOUSD,CRUDE,CL,OILUSD,XTIUSD,WTIUSD,USCrude,CrudeOIL";
   if(d == "UKOIL")  return "BRENT,UKOUSD,XBRUSD,BRENTUSD,UKBrent";
   if(d == "NATGAS") return "NGAS,NAT.GAS,XNGUSD,NaturalGas,NGC";
   if(d == "XAUUSD") return "GOLD,GOLDUSD,XAUUSDm";
   if(d == "XAGUSD") return "SILVER,SILVERUSD,XAGUSDm";
   if(d == "XPTUSD") return "PLATINUM,PLATINUMUSD";
   if(d == "BTCUSD") return "XBTUSD,BITCOIN,BTCUSDT,BTCUSD.m,BTCUSDm";
   if(d == "ETHUSD") return "ETHEREUM,ETHUSDT,ETHUSD.m,ETHUSDm";
   if(d == "LTCUSD") return "LITECOIN,LTCUSDT,LTCUSDm";
   if(d == "XRPUSD") return "RIPPLE,XRPUSDT,XRPUSDm";
   if(d == "BNBUSD") return "BNBUSDT,BNBUSDm";
   if(d == "SOLUSD") return "SOLUSDT,SOLUSDm";
   return "";
  }

//+------------------------------------------------------------------+
//| Scan the cached broker index for a symbol whose normalized name   |
//| equals, or starts with (suffix case), the target. "" if none.     |
//+------------------------------------------------------------------+
string FindBrokerByNorm(const string target)
  {
   string tnorm = NormalizeSym(target);
   if(StringLen(tnorm) == 0) return "";
   for(int i = 0; i < g_bsymCount; i++)
     {
      string bn = g_bsymNorm[i];
      if(bn == tnorm) return g_bsymName[i];
      if(StringLen(bn) > StringLen(tnorm) && StringFind(bn, tnorm) == 0)
         return g_bsymName[i];        // e.g. broker "US30CASH" matches "US30"
     }
   return "";
  }

//+------------------------------------------------------------------+
//| Resolve a dashboard symbol name to THIS broker's actual symbol.   |
//| Priority: explicit user alias > exact > normalized broker match > |
//| known synonyms. Returns the input unchanged if nothing matches.   |
//+------------------------------------------------------------------+
string ResolveSymbol(const string dash)
  {
   // 1. Explicit user-provided alias (SymbolAliases input) wins.
   for(int i = 0; i < g_aliasCount; i++)
      if(StringCompare(g_aliasFrom[i], dash, false) == 0)
         return g_aliasTo[i];

   // 2. Exact name exists at the broker.
   if(SymbolSelect(dash, true)) return dash;

   if(!g_bsymBuilt) BuildBrokerSymbolIndex();

   // 3. Normalized match (handles .cash / .m / # prefixes / case).
   string hit = FindBrokerByNorm(dash);
   if(hit != "") return hit;

   // 4. Known cross-broker synonyms (US30->DJ30, USOIL->BRENT? no; WTI etc).
   string cand[];
   int n = StringSplit(SynonymsCSV(dash), ',', cand);
   for(int i = 0; i < n; i++)
     {
      string c = cand[i];
      StringTrimLeft(c); StringTrimRight(c);
      if(StringLen(c) == 0) continue;
      if(SymbolSelect(c, true)) return c;     // exact synonym present
      hit = FindBrokerByNorm(c);              // synonym with a suffix
      if(hit != "") return hit;
     }

   // 5. Give up — return as-is (FetchEnabledPairs will skip if unavailable).
   return dash;
  }

//+------------------------------------------------------------------+
//| Fetch the dashboard's enabled pairs (GET /api/pairs) and store    |
//| the ones this broker actually offers in g_dynamicSymbols[].       |
//| On any failure the previous list is kept (graceful degradation).  |
//+------------------------------------------------------------------+
void FetchEnabledPairs()
  {
   if(!UseDashboardPairs) return;

   char   body[];
   char   result[];
   string respHeaders;
   ArrayResize(body, 0);

   int httpCode = WebRequest("GET", g_pairsURL, "", 5000, body, result, respHeaders);
   if(httpCode != 200)
     {
      if(VerboseLog)
         Print("SLCDataBridge: pairs fetch failed HTTP=", httpCode, " error=", GetLastError());
      return;
     }

   string resp = CharArrayToString(result, 0, WHOLE_ARRAY);
   // Expected: {"enabled_pairs":["EURUSD","GBPUSD",...],"count":N}
   int start = StringFind(resp, "[");
   int end   = (start >= 0) ? StringFind(resp, "]", start) : -1;
   if(start < 0 || end <= start)
     {
      if(VerboseLog) Print("SLCDataBridge: could not parse pairs response.");
      return;
     }

   string inner = StringSubstr(resp, start + 1, end - start - 1);   // "EURUSD","GBPUSD",...
   string parts[];
   int n = StringSplit(inner, ',', parts);

   string vdash[];     // dashboard names (kept as the identity)
   string vbrok[];     // matching broker symbols
   int vc = 0;
   int skipped = 0;
   for(int i = 0; i < n; i++)
     {
      string s = parts[i];
      StringReplace(s, "\"", "");
      StringTrimLeft(s);
      StringTrimRight(s);
      if(StringLen(s) == 0) continue;
      // Auto-resolve the dashboard name to this broker's actual symbol.
      string broker = ResolveSymbol(s);
      // Skip anything the broker doesn't actually offer (so an index/crypto
      // it doesn't carry can't break the push).
      if(!SymbolSelect(broker, true))
        {
         skipped++;
         if(VerboseLog) Print("SLCDataBridge: skipping unavailable symbol '", s,
                              (s == broker ? "" : "' (tried '" + broker + "')"), "'");
         continue;
        }
      ArrayResize(vdash, vc + 1);
      ArrayResize(vbrok, vc + 1);
      vdash[vc] = s;        // emit the DASHBOARD name downstream
      vbrok[vc] = broker;   // use the BROKER name for MT5 calls
      vc++;
      if(VerboseLog && s != broker)
         Print("SLCDataBridge: mapped '", s, "' -> broker '", broker, "'");
     }

   if(vc > 0)
     {
      ArrayResize(g_dynamicSymbols, vc);
      ArrayResize(g_dynBroker, vc);
      for(int i = 0; i < vc; i++)
        {
         g_dynamicSymbols[i] = vdash[i];
         g_dynBroker[i]      = vbrok[i];
        }
      g_dynCount = vc;
      if(VerboseLog)
         Print("SLCDataBridge: watching ", vc, " dashboard-enabled symbol(s), ", skipped, " skipped.");
     }
   else
     {
      g_dynCount = 0;
      if(VerboseLog)
         Print("SLCDataBridge: no usable enabled pairs — using static fallback lists.");
     }
  }

//+------------------------------------------------------------------+
//| Account + price data push → /api/mt5_feed                        |
//+------------------------------------------------------------------+
void PushData()
  {
   string json = BuildPayload();
   if(json == "") return;

   char   body[];
   char   result[];
   string respHeaders;

   int bodyLen = StringToCharArray(json, body, 0, WHOLE_ARRAY) - 1;
   if(bodyLen <= 0) return;
   ArrayResize(body, bodyLen);

   int httpCode = WebRequest(
      "POST",
      g_feedURL,
      "Content-Type: application/json\r\n",
      5000,
      body,
      result,
      respHeaders
   );

   if(httpCode == 200)
     {
      if(VerboseLog)
         Print("SLCDataBridge: push OK (", bodyLen, " bytes)");
     }
   else
     {
      Print("SLCDataBridge: push failed. HTTP=", httpCode, " | error=", GetLastError());
     }
  }

//+------------------------------------------------------------------+
//| OHLCV bar push → /api/mt5_bars                                   |
//+------------------------------------------------------------------+
string g_pushedSyms = "";   // "|SYM|" markers: full history already sent

void PushBars()
  {
   string dashSyms[];
   string brokerSyms[];
   int symCount = BuildWatchPairs(BarSymbols, dashSyms, brokerSyms);
   if(symCount == 0) return;

   // MT5 server UTC offset so Python can normalise timestamps
   int tzOffsetSec = (int)(TimeCurrent() - TimeGMT());

   // ONE REQUEST PER SYMBOL. Building a single payload for many
   // symbols (46 syms x 6 TFs x 300 bars ~ 6-7 MB through 80k+ string
   // concatenations) can exhaust EA memory and gets the EA removed from
   // the chart with a critical error. Per-symbol chunks stay ~150 KB.
   // After a symbol's full history is accepted once, only the last 10
   // bars per TF are sent (the server upserts, so this is lossless).
   int okCount = 0;
   for(int s = 0; s < symCount; s++)
     {
      string dashKey = dashSyms[s];      // JSON key (engine/dashboard identity)
      string sym     = brokerSyms[s];    // broker symbol for MT5 calls
      if(StringLen(dashKey) == 0 || StringLen(sym) == 0) continue;

      SymbolSelect(sym, true);

      bool fullPush = (StringFind(g_pushedSyms, "|" + dashKey + "|") < 0);
      int  nBars    = fullPush ? BarsToSend : 10;

      string symBars15 = SerializeBarsForSymbol(sym, PERIOD_M15, nBars);
      string symBars30 = SerializeBarsForSymbol(sym, PERIOD_M30, nBars);
      string symBars1h = SerializeBarsForSymbol(sym, PERIOD_H1,  nBars);
      string symBars2h = SerializeBarsForSymbol(sym, PERIOD_H2,  nBars);
      string symBars4h = SerializeBarsForSymbol(sym, PERIOD_H4,  nBars);
      string symBars1d = SerializeBarsForSymbol(sym, PERIOD_D1,  nBars);

      if(symBars15 == "[]" && symBars30 == "[]" && symBars1h == "[]" &&
         symBars2h == "[]" && symBars4h == "[]" && symBars1d == "[]") continue;

      string j = "{";
      j += "\"tz_offset_sec\":" + IntegerToString(tzOffsetSec) + ",";
      j += "\"bars\":{";
      j += "\"" + dashKey + "\":{";
      j += "\"15m\":" + symBars15 + ",";
      j += "\"30m\":" + symBars30 + ",";
      j += "\"1h\":"  + symBars1h + ",";
      j += "\"2h\":"  + symBars2h + ",";
      j += "\"4h\":"  + symBars4h + ",";
      j += "\"1d\":"  + symBars1d;
      j += "}}}";

      char   body[];
      char   result[];
      string respHeaders;
      int    bodyLen = StringToCharArray(j, body, 0, WHOLE_ARRAY) - 1;
      if(bodyLen <= 0) continue;
      ArrayResize(body, bodyLen);

      int httpCode = WebRequest(
         "POST",
         g_barsURL,
         "Content-Type: application/json\r\n",
         10000,
         body,
         result,
         respHeaders
      );

      if(httpCode == 200)
        {
         okCount++;
         if(fullPush)
            g_pushedSyms += "|" + dashKey + "|";
        }
      else
        {
         Print("SLCDataBridge: bars push failed for ", dashKey,
               ". HTTP=", httpCode, " | error=", GetLastError());
        }
     }

   if(VerboseLog)
      Print("SLCDataBridge: bars push OK for ", okCount, "/", symCount, " symbol(s)");
  }

//--- Serialise up to `count` closed bars for one symbol+period as a JSON array.
string SerializeBarsForSymbol(const string sym, ENUM_TIMEFRAMES period, int count)
  {
   MqlRates rates[];
   int copied = CopyRates(sym, period, 1, count, rates);  // offset=1 skips the current open bar
   if(copied <= 0) return "[]";

   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);

   string j = "[";
   for(int i = 0; i < copied; i++)
     {
      if(i > 0) j += ",";
      j += "{";
      j += "\"t\":" + IntegerToString((int)rates[i].time)        + ",";
      j += "\"o\":" + DoubleToString(rates[i].open,  digits)     + ",";
      j += "\"h\":" + DoubleToString(rates[i].high,  digits)     + ",";
      j += "\"l\":" + DoubleToString(rates[i].low,   digits)     + ",";
      j += "\"c\":" + DoubleToString(rates[i].close, digits)     + ",";
      j += "\"v\":" + IntegerToString((int)rates[i].tick_volume);
      j += "}";
     }
   j += "]";
   return j;
  }

//+------------------------------------------------------------------+
//| Command polling and execution                                     |
//+------------------------------------------------------------------+
void PollAndExecuteCommand()
  {
   char   body[];
   char   result[];
   string respHeaders;

   ArrayResize(body, 0);  // empty body for GET

   int httpCode = WebRequest(
      "GET",
      g_commandsURL,
      "",
      5000,
      body,
      result,
      respHeaders
   );

   if(httpCode != 200)
     {
      if(VerboseLog)
         Print("SLCDataBridge: command poll failed HTTP=", httpCode, " error=", GetLastError());
      return;
     }

   string resp = CharArrayToString(result, 0, WHOLE_ARRAY);
   if(VerboseLog)
      Print("SLCDataBridge: command response: ", StringSubstr(resp, 0, 200));

   if(StringFind(resp, "\"command\":null") >= 0 ||
      StringFind(resp, "\"command\": null") >= 0)
     {
      if(VerboseLog) Print("SLCDataBridge: no pending commands.");
      return;
     }

   string cmdId   = ExtractStringField(resp, "id");
   string cmdType = ExtractStringField(resp, "type");
   string symbol  = ExtractStringField(resp, "symbol");
   long   ticket  = (long)ExtractNumberField(resp, "ticket");
   double new_sl  = ExtractNumberField(resp, "new_sl");
   string reason  = ExtractStringField(resp, "reason");

   if(cmdId == "" || cmdType == "")
     {
      Print("SLCDataBridge: malformed command — id=", cmdId, " type=", cmdType);
      return;
     }

   Print("SLCDataBridge: executing ", cmdType, " | ticket=", ticket,
         " | reason=", StringSubstr(reason, 0, 100));

   bool ok = false;

   if(cmdType == "trail_sl" || cmdType == "move_sl_be")
     {
      if(ticket == 0) { Print("SLCDataBridge: SL command missing ticket."); return; }
      ok = ExecuteSLModify(ticket, new_sl, reason);
     }
   else if(cmdType == "open_trade")
      ok = ExecuteOpen(resp, symbol, reason);
   else if(cmdType == "close_trade")
      ok = ExecuteClose(resp, ticket, reason);
   else
     {
      Print("SLCDataBridge: unknown command type '", cmdType, "' — skipping.");
      ok = true;  // Acknowledge so it doesn't loop
     }

   if(ok)
      AcknowledgeCommand(cmdId);
  }

//+------------------------------------------------------------------+
//| v2.30: Open a market position from a server command.              |
//| Expected fields: symbol, side ("buy"/"sell"), lots, sl, tp,       |
//| magic, comment. Guarded by AllowTradeExecution / MaxLotsPerTrade  |
//| / MaxOpenPositions. Always returns true on a *deliberate* refusal |
//| so the command is acked and does not loop.                        |
//+------------------------------------------------------------------+
bool ExecuteOpen(const string &resp, string dashSymbol, string reason)
  {
   if(!AllowTradeExecution)
     {
      Print("SLCDataBridge: open_trade REFUSED — AllowTradeExecution=false (EA inputs).");
      return true;   // ack so the server learns via feed that nothing opened
     }
   if(PositionsTotal() >= MaxOpenPositions)
     {
      Print("SLCDataBridge: open_trade REFUSED — MaxOpenPositions (", MaxOpenPositions, ") reached.");
      return true;
     }

   string side    = ExtractStringField(resp, "side");
   double lots    = ExtractNumberField(resp, "lots");
   double sl      = ExtractNumberField(resp, "sl");
   double tp      = ExtractNumberField(resp, "tp");
   long   magic   = (long)ExtractNumberField(resp, "magic");
   string comment = ExtractStringField(resp, "comment");

   if(dashSymbol == "" || (side != "buy" && side != "sell") || lots <= 0)
     {
      Print("SLCDataBridge: open_trade malformed — symbol=", dashSymbol, " side=", side, " lots=", lots);
      return true;
     }

   string broker = ResolveSymbol(dashSymbol);
   if(!SymbolSelect(broker, true))
     {
      Print("SLCDataBridge: open_trade — symbol unavailable: ", dashSymbol);
      return true;
     }

   // Clamp lots: server request -> broker limits -> hard cap.
   double minLot  = SymbolInfoDouble(broker, SYMBOL_VOLUME_MIN);
   double maxLot  = SymbolInfoDouble(broker, SYMBOL_VOLUME_MAX);
   double lotStep = SymbolInfoDouble(broker, SYMBOL_VOLUME_STEP);
   lots = MathMin(lots, MaxLotsPerTrade);
   lots = MathMin(lots, maxLot);
   lots = MathMax(lots, minLot);
   if(lotStep > 0) lots = MathFloor(lots / lotStep) * lotStep;
   lots = NormalizeDouble(lots, 2);

   int digits = (int)SymbolInfoInteger(broker, SYMBOL_DIGITS);
   sl = (sl > 0) ? NormalizeDouble(sl, digits) : 0;
   tp = (tp > 0) ? NormalizeDouble(tp, digits) : 0;

   g_trade.SetExpertMagicNumber((ulong)magic);
   bool ok;
   if(side == "buy")
      ok = g_trade.Buy(lots, broker, 0, sl, tp, comment);
   else
      ok = g_trade.Sell(lots, broker, 0, sl, tp, comment);

   if(ok)
      Print("SLCDataBridge: OPENED ✓ ", side, " ", DoubleToString(lots, 2), " ", broker,
            " sl=", sl, " tp=", tp, " ticket=", g_trade.ResultOrder(), " | ", StringSubstr(reason, 0, 80));
   else
      Print("SLCDataBridge: open FAILED ", broker,
            " retcode=", g_trade.ResultRetcode(), " comment=", g_trade.ResultComment());

   return ok;
  }

//+------------------------------------------------------------------+
//| v2.30: Close a position (full or partial) from a server command.  |
//| Expected fields: ticket, lots (0 or missing = close full).        |
//+------------------------------------------------------------------+
bool ExecuteClose(const string &resp, long ticket, string reason)
  {
   if(!AllowTradeExecution)
     {
      Print("SLCDataBridge: close_trade REFUSED — AllowTradeExecution=false (EA inputs).");
      return true;
     }
   if(ticket == 0)
     {
      Print("SLCDataBridge: close_trade missing ticket.");
      return true;
     }
   if(!PositionSelectByTicket((ulong)ticket))
     {
      Print("SLCDataBridge: close_trade — ticket ", ticket, " not found (already closed?).");
      return true;   // ack: nothing to do
     }

   double posLots   = PositionGetDouble(POSITION_VOLUME);
   double closeLots = ExtractNumberField(resp, "lots");
   string broker    = PositionGetString(POSITION_SYMBOL);

   bool ok;
   if(closeLots > 0 && closeLots < posLots)
     {
      double lotStep = SymbolInfoDouble(broker, SYMBOL_VOLUME_STEP);
      if(lotStep > 0) closeLots = MathFloor(closeLots / lotStep) * lotStep;
      double minLot = SymbolInfoDouble(broker, SYMBOL_VOLUME_MIN);
      if(closeLots < minLot) closeLots = minLot;
      ok = g_trade.PositionClosePartial((ulong)ticket, NormalizeDouble(closeLots, 2));
      if(ok) Print("SLCDataBridge: PARTIAL CLOSE ✓ ticket=", ticket, " lots=", closeLots, " | ", StringSubstr(reason, 0, 80));
     }
   else
     {
      ok = g_trade.PositionClose((ulong)ticket);
      if(ok) Print("SLCDataBridge: CLOSED ✓ ticket=", ticket, " | ", StringSubstr(reason, 0, 80));
     }

   if(!ok)
      Print("SLCDataBridge: close FAILED ticket=", ticket,
            " retcode=", g_trade.ResultRetcode(), " comment=", g_trade.ResultComment());
   return ok;
  }

//+------------------------------------------------------------------+
//| Execute a Stop-Loss modification for an open position            |
//+------------------------------------------------------------------+
bool ExecuteSLModify(long ticket, double new_sl, string reason)
  {
   if(new_sl <= 0)
     {
      Print("SLCDataBridge: invalid new_sl=", new_sl, " for ticket=", ticket);
      return false;
     }

   if(!PositionSelectByTicket((ulong)ticket))
     {
      Print("SLCDataBridge: cannot select ticket=", ticket, " (may be closed). Error=", GetLastError());
      return false;
     }

   double current_sl = PositionGetDouble(POSITION_SL);
   double current_tp = PositionGetDouble(POSITION_TP);
   string pos_symbol = PositionGetString(POSITION_SYMBOL);
   string pos_side   = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? "BUY" : "SELL";

   if(pos_side == "BUY" && new_sl < current_sl && current_sl > 0)
     {
      Print("SLCDataBridge: SAFETY — refusing to move BUY SL backward. current_sl=", current_sl, " new_sl=", new_sl);
      return true;
     }
   if(pos_side == "SELL" && new_sl > current_sl && current_sl > 0)
     {
      Print("SLCDataBridge: SAFETY — refusing to move SELL SL backward. current_sl=", current_sl, " new_sl=", new_sl);
      return true;
     }

   int digits = (int)SymbolInfoInteger(pos_symbol, SYMBOL_DIGITS);
   new_sl = NormalizeDouble(new_sl, digits);

   bool ok = g_trade.PositionModify((ulong)ticket, new_sl, current_tp);

   if(ok)
      Print("SLCDataBridge: SL modified ✓ ticket=", ticket, " symbol=", pos_symbol,
            " side=", pos_side, " old_sl=", current_sl, " → new_sl=", new_sl);
   else
      Print("SLCDataBridge: SL modify FAILED ticket=", ticket,
            " retcode=", g_trade.ResultRetcode(), " comment=", g_trade.ResultComment());

   return ok;
  }

//+------------------------------------------------------------------+
//| Acknowledge a command via POST /api/commands/ack/{id}            |
//+------------------------------------------------------------------+
void AcknowledgeCommand(string cmdId)
  {
   string ackURL = g_ackBase + cmdId;

   char   body[];
   char   result[];
   string respHeaders;
   ArrayResize(body, 0);

   int httpCode = WebRequest(
      "POST",
      ackURL,
      "Content-Type: application/json\r\n",
      5000,
      body,
      result,
      respHeaders
   );

   if(httpCode == 200)
      Print("SLCDataBridge: command acked ✓ id=", cmdId);
   else
      Print("SLCDataBridge: ack failed HTTP=", httpCode, " id=", cmdId);
  }

//+------------------------------------------------------------------+
//| JSON field extractors (simple substring-based, no library)       |
//+------------------------------------------------------------------+
string ExtractStringField(const string &json, const string &key)
  {
   string search = "\"" + key + "\":\"";
   int start = StringFind(json, search);
   if(start < 0) return "";
   start += StringLen(search);
   int end = StringFind(json, "\"", start);
   if(end < 0) return "";
   return StringSubstr(json, start, end - start);
  }

double ExtractNumberField(const string &json, const string &key)
  {
   string search = "\"" + key + "\":";
   int start = StringFind(json, search);
   if(start < 0) return 0.0;
   start += StringLen(search);
   string numStr = "";
   int maxLen = MathMin(start + 30, StringLen(json));
   for(int i = start; i < maxLen; i++)
     {
      ushort ch = StringGetCharacter(json, i);
      if(ch == '-' || ch == '.' || (ch >= '0' && ch <= '9'))
         numStr += ShortToString(ch);
      else
         break;
     }
   if(numStr == "") return 0.0;
   return StringToDouble(numStr);
  }

//+------------------------------------------------------------------+
//| Build the account/position/price data payload                    |
//+------------------------------------------------------------------+
//+------------------------------------------------------------------+
//| Tick-accurate worst-case extremes for one symbol from last push to now.
//| Lowest bid, highest ask and max spread (points) over EVERY tick since
//| the last push. Falls back to the live quote if no ticks are ready.   |
//+------------------------------------------------------------------+
void SpanExtremes(const string sym, const ulong fromMsc,
                  double &minBid, double &maxAsk, double &maxSprPts, double &point)
  {
   point         = SymbolInfoDouble(sym, SYMBOL_POINT);
   double curBid = SymbolInfoDouble(sym, SYMBOL_BID);
   double curAsk = SymbolInfoDouble(sym, SYMBOL_ASK);
   minBid    = curBid;
   maxAsk    = curAsk;
   maxSprPts = (point > 0) ? (curAsk - curBid) / point : 0;

   ulong floorMsc = (ulong)TimeCurrent() * 1000 - (ulong)(PushIntervalSec + 2) * 1000;
   ulong from     = (fromMsc > floorMsc) ? fromMsc : floorMsc;

   MqlTick ticks[];
   int n = CopyTicks(sym, ticks, COPY_TICKS_ALL, from, 0);
   for(int i = 0; i < n; i++)
     {
      double b = ticks[i].bid, a = ticks[i].ask;
      if(b > 0 && b < minBid) minBid = b;
      if(a > 0 && a > maxAsk) maxAsk = a;
      if(b > 0 && a > 0 && point > 0)
        {
         double sp = (a - b) / point;
         if(sp > maxSprPts) maxSprPts = sp;
        }
     }
  }

string BuildPayload()
  {
   string j = "{";

   //--- Account info
   j += "\"account\":{";
   j += "\"login\":"        + IntegerToString((int)AccountInfoInteger(ACCOUNT_LOGIN))    + ",";
   j += "\"balance\":"      + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2)      + ",";
   j += "\"equity\":"       + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2)       + ",";
   j += "\"margin\":"       + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN), 2)       + ",";
   j += "\"free_margin\":"  + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2)  + ",";
   j += "\"margin_level\":" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_LEVEL), 2) + ",";
   j += "\"profit\":"       + DoubleToString(AccountInfoDouble(ACCOUNT_PROFIT), 2)       + ",";
   j += "\"currency\":\""   + EscapeJSON(AccountInfoString(ACCOUNT_CURRENCY))            + "\",";
   j += "\"broker\":\""     + EscapeJSON(AccountInfoString(ACCOUNT_COMPANY))             + "\",";
   j += "\"server\":\""     + EscapeJSON(AccountInfoString(ACCOUNT_SERVER))              + "\",";
   j += "\"leverage\":"     + IntegerToString((int)AccountInfoInteger(ACCOUNT_LEVERAGE));
   j += "},";

   //--- Open positions
   j += "\"open_positions\":[";
   int posTotal = PositionsTotal();
   for(int i = 0; i < posTotal; i++)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;

      ENUM_POSITION_TYPE pType = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      string side = (pType == POSITION_TYPE_BUY) ? "buy" : "sell";

      if(i > 0) j += ",";
      j += "{";
      j += "\"ticket\":"         + IntegerToString((int)ticket)                                           + ",";
      j += "\"symbol\":\""       + EscapeJSON(PositionGetString(POSITION_SYMBOL))                         + "\",";
      j += "\"side\":\""         + side                                                                   + "\",";
      j += "\"lots\":"           + DoubleToString(PositionGetDouble(POSITION_VOLUME), 2)                  + ",";
      j += "\"entry\":"          + DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN), 5)              + ",";
      j += "\"sl\":"             + DoubleToString(PositionGetDouble(POSITION_SL), 5)                      + ",";
      j += "\"tp\":"             + DoubleToString(PositionGetDouble(POSITION_TP), 5)                      + ",";
      j += "\"current\":"        + DoubleToString(PositionGetDouble(POSITION_PRICE_CURRENT), 5)           + ",";
      j += "\"unrealized_pnl\":" + DoubleToString(PositionGetDouble(POSITION_PROFIT), 2)                  + ",";
      j += "\"swap\":"           + DoubleToString(PositionGetDouble(POSITION_SWAP), 2)                    + ",";
      j += "\"open_time\":"      + IntegerToString((int)PositionGetInteger(POSITION_TIME))                + ",";
      j += "\"magic\":"          + IntegerToString((int)PositionGetInteger(POSITION_MAGIC))               + ",";
      j += "\"comment\":\""      + EscapeJSON(PositionGetString(POSITION_COMMENT))                       + "\"";
      j += "}";
     }
   j += "],";

   //--- Closed trades (last N days)
   datetime since = TimeCurrent() - (datetime)(ClosedTradesDays * 86400);
   if(!HistorySelect(since, TimeCurrent()))
      Print("SLCDataBridge: HistorySelect failed, error=", GetLastError());

   j += "\"closed_today\":[";
   int  dealTotal = HistoryDealsTotal();
   bool firstDeal = true;
   for(int i = dealTotal - 1; i >= 0; i--)
     {
      ulong dTicket = HistoryDealGetTicket(i);
      if(dTicket == 0) continue;

      ENUM_DEAL_ENTRY entryType = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(dTicket, DEAL_ENTRY);
      if(entryType != DEAL_ENTRY_OUT) continue;

      ENUM_DEAL_TYPE dealType = (ENUM_DEAL_TYPE)HistoryDealGetInteger(dTicket, DEAL_TYPE);
      if(dealType != DEAL_TYPE_BUY && dealType != DEAL_TYPE_SELL) continue;

      string side = (dealType == DEAL_TYPE_BUY) ? "buy" : "sell";

      if(!firstDeal) j += ",";
      firstDeal = false;

      j += "{";
      j += "\"ticket\":"      + IntegerToString((int)dTicket)                                           + ",";
      j += "\"position_id\":" + IntegerToString((int)HistoryDealGetInteger(dTicket, DEAL_POSITION_ID))  + ",";
      j += "\"symbol\":\""    + EscapeJSON(HistoryDealGetString(dTicket, DEAL_SYMBOL))                  + "\",";
      j += "\"side\":\""      + side                                                                    + "\",";
      j += "\"lots\":"        + DoubleToString(HistoryDealGetDouble(dTicket, DEAL_VOLUME), 2)           + ",";
      j += "\"price\":"       + DoubleToString(HistoryDealGetDouble(dTicket, DEAL_PRICE), 5)            + ",";
      j += "\"sl\":"          + DoubleToString(HistoryDealGetDouble(dTicket, DEAL_SL), 5)               + ",";
      j += "\"tp\":"          + DoubleToString(HistoryDealGetDouble(dTicket, DEAL_TP), 5)               + ",";
      j += "\"profit\":"      + DoubleToString(HistoryDealGetDouble(dTicket, DEAL_PROFIT), 2)           + ",";
      j += "\"commission\":"  + DoubleToString(HistoryDealGetDouble(dTicket, DEAL_COMMISSION), 2)       + ",";
      j += "\"swap\":"        + DoubleToString(HistoryDealGetDouble(dTicket, DEAL_SWAP), 2)             + ",";
      j += "\"time\":"        + IntegerToString((int)HistoryDealGetInteger(dTicket, DEAL_TIME))         + ",";
      j += "\"magic\":"       + IntegerToString((int)HistoryDealGetInteger(dTicket, DEAL_MAGIC))        + ",";
      j += "\"comment\":\""   + EscapeJSON(HistoryDealGetString(dTicket, DEAL_COMMENT))                + "\"";
      j += "}";
     }
   j += "],";

   //--- Live prices for watched symbols
   string pDash[];
   string pBroker[];
   int    watchCount = BuildWatchPairs(SymbolsToWatch, pDash, pBroker);
   j += "\"prices\":[";
   bool firstPrice = true;
   for(int i = 0; i < watchCount; i++)
     {
      string dashKey = pDash[i];     // identity emitted downstream
      string sym     = pBroker[i];   // broker symbol for MT5 calls
      if(StringLen(dashKey) == 0 || StringLen(sym) == 0) continue;

      SymbolSelect(sym, true);

      double bid = SymbolInfoDouble(sym, SYMBOL_BID);
      double ask = SymbolInfoDouble(sym, SYMBOL_ASK);
      if(bid <= 0 && ask <= 0) continue;

      int    digits    = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      double point     = SymbolInfoDouble(sym, SYMBOL_POINT);
      double spread    = point > 0 ? NormalizeDouble((ask - bid) / point, 1) : 0;
      double minBid, maxAsk, maxSprPts, pointSz;
      SpanExtremes(sym, g_lastSpanMsc, minBid, maxAsk, maxSprPts, pointSz);
      double dayHigh   = SymbolInfoDouble(sym, SYMBOL_BIDHIGH);
      double dayLow    = SymbolInfoDouble(sym, SYMBOL_BIDLOW);
      double dayOpen   = iOpen(sym, PERIOD_D1, 0);
      double changePct = (dayOpen > 0 && bid > 0)
                         ? NormalizeDouble((bid - dayOpen) / dayOpen * 100.0, 3)
                         : 0.0;
      // Broker-exact sizing inputs: account-ccy value of one tick per 1 lot,
      // and the tick (price) size. The Python risk engine uses these so cross
      // pairs size correctly without a hand-tuned pip_value.
      double tickValue = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE);
      double tickSize  = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);

      if(!firstPrice) j += ",";
      firstPrice = false;
      j += "{";
      j += "\"symbol\":\""   + EscapeJSON(dashKey)              + "\",";
      j += "\"bid\":"        + DoubleToString(bid, digits)      + ",";
      j += "\"ask\":"        + DoubleToString(ask, digits)      + ",";
      j += "\"spread\":"     + DoubleToString(spread, 1)        + ",";
      j += "\"min_bid\":"    + DoubleToString(minBid, digits)    + ",";
      j += "\"max_ask\":"    + DoubleToString(maxAsk, digits)    + ",";
      j += "\"max_spread\":" + DoubleToString(maxSprPts, 1)      + ",";
      j += "\"point\":"      + DoubleToString(pointSz, 8)        + ",";
      j += "\"day_high\":"   + DoubleToString(dayHigh, digits)  + ",";
      j += "\"day_low\":"    + DoubleToString(dayLow, digits)   + ",";
      j += "\"day_open\":"   + DoubleToString(dayOpen, digits)  + ",";
      j += "\"change_pct\":" + DoubleToString(changePct, 3)     + ",";
      j += "\"digits\":"     + IntegerToString(digits)          + ",";
      j += "\"tick_value\":" + DoubleToString(tickValue, 5)     + ",";
      j += "\"tick_size\":"  + DoubleToString(tickSize, 8);
      j += "}";
     }
   g_lastSpanMsc = (ulong)TimeCurrent() * 1000;   // next push covers a fresh span
   j += "],";

   //--- Metadata
   j += "\"terminal\":{";
   j += "\"platform\":\"MT5\",";
   j += "\"version\":\"2.30\",";
   j += "\"time_utc\":"   + IntegerToString((int)TimeGMT())     + ",";
   j += "\"time_local\":" + IntegerToString((int)TimeCurrent());
   j += "}";

   j += "}";
   return j;
  }

//+------------------------------------------------------------------+
//| Escape special characters for JSON strings                       |
//+------------------------------------------------------------------+
string EscapeJSON(string s)
  {
   StringReplace(s, "\\", "\\\\");
   StringReplace(s, "\"", "\\\"");
   StringReplace(s, "\n", "\\n");
   StringReplace(s, "\r", "\\r");
   StringReplace(s, "\t", "\\t");
   return s;
  }
//+------------------------------------------------------------------+
