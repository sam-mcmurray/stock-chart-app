import { Link, useInRouterContext } from 'react-router-dom';
import {
  fmtMoney,
  fmtPct,
  tradeReturnPct,
} from '../../lib/calendarUtils';
import { SummaryTile } from './CalendarPanels';

function SymbolLink({ symbol }) {
  const safeSymbol = symbol || 'UNKNOWN';
  const inRouter = useInRouterContext();

  if (!symbol) return safeSymbol;

  const to = `/scanner?symbol=${encodeURIComponent(symbol)}`;

  if (!inRouter) {
    return (
      <a className="symbol-link" href={to}>
        {safeSymbol}
      </a>
    );
  }

  return (
    <Link className="symbol-link" to={to}>
      {safeSymbol}
    </Link>
  );
}

export function TradeDayDetail({ activeDay }) {
  const tradeCount = activeDay.trades.length;
  const wins = activeDay.trades.filter((trade) => Number(trade.pl || 0) > 0).length;
  const losses = activeDay.trades.filter((trade) => Number(trade.pl || 0) < 0).length;
  const winRate = tradeCount ? (wins / tradeCount) * 100 : null;
  const avgPl = tradeCount ? activeDay.pl / tradeCount : 0;

  return (
    <div className="panel trade-day-detail">
      <h3 className="page-title">{activeDay.label}</h3>
      <div className="trade-day-metrics">
        <SummaryTile label="Total P/L" value={fmtMoney(activeDay.pl)} tone={activeDay.pl >= 0 ? 'gain' : 'loss'} />
        <SummaryTile label="Trades" value={tradeCount.toLocaleString()} />
        <SummaryTile label="Win rate" value={winRate === null ? 'N/A' : fmtPct(winRate)} tone="gain" />
        <SummaryTile label="Avg P/L" value={fmtMoney(avgPl)} tone={avgPl >= 0 ? 'gain' : 'loss'} />
        <SummaryTile label="Wins" value={wins.toLocaleString()} tone="gain" />
        <SummaryTile label="Losses" value={losses.toLocaleString()} tone="loss" />
      </div>

      {activeDay.trades.length ? (
        <div className="table-scroll">
          <table className="dashboard-table trade-detail-table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Qty</th>
                <th>Entry</th>
                <th>Exit</th>
                <th>P/L</th>
                <th>Return</th>
                <th>Bucket</th>
              </tr>
            </thead>
            <tbody>
              {activeDay.trades.map((trade, index) => {
                const returnPct = tradeReturnPct(trade);

                return (
                  <tr key={`${trade.symbol}-${index}`}>
                    <td className="symbol-cell">
                      <SymbolLink symbol={trade.symbol} />
                    </td>
                    <td>{trade.qty}</td>
                    <td>{fmtMoney(trade.entry)}</td>
                    <td>{fmtMoney(trade.exit)}</td>
                    <td className={(trade.pl || 0) >= 0 ? 'gain' : 'loss'}>{fmtMoney(trade.pl)}</td>
                    <td className={(returnPct || 0) >= 0 ? 'gain' : 'loss'}>{fmtPct(returnPct)}</td>
                    <td className="muted-cell">{trade.bucket || 'N/A'}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="empty-panel">No closed trades on this day.</div>
      )}
    </div>
  );
}
