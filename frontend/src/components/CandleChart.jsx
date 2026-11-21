import { useEffect, useMemo, useRef } from 'react';
import {
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  createChart,
  createSeriesMarkers,
} from 'lightweight-charts';
import { calculateMacd, calculateVwap, normalizeChartCandles } from '../lib/chartIndicators';

export default function CandleChart({
  candles = [],
  height = 280,
  indicators = false,
  daily = false,
  visibleBars = 120,
  newsEvents = [],
}) {
  const elRef = useRef(null);
  const chartRef = useRef(null);
  const candleSeriesRef = useRef(null);
  const volumeSeriesRef = useRef(null);
  const indicatorSeriesRef = useRef({});
  const newsMarkersRef = useRef(null);
  const dataWindowRef = useRef(null);
  const chartData = useMemo(() => normalizeChartCandles(candles), [candles]);
  const volumeData = useMemo(
    () => chartData.map((candle) => ({
      time: candle.time,
      value: candle.volume,
      color: candle.close >= candle.open ? 'rgba(45,212,191,.35)' : 'rgba(248,113,113,.35)',
    })),
    [chartData],
  );
  const vwapData = useMemo(() => calculateVwap(chartData, !daily), [chartData, daily]);
  const macdData = useMemo(() => calculateMacd(chartData), [chartData]);
  const hasData = chartData.length > 0;
  const newsMarkers = useMemo(() => {
    if (!chartData.length) return [];
    const grouped = new Map();
    for (const event of Array.isArray(newsEvents) ? newsEvents : []) {
      const published = Number(event?.published_ts || event?.publishedAt || 0);
      if (!Number.isFinite(published) || published <= 0) continue;
      const eventTime = published > 10_000_000_000 ? Math.floor(published / 1000) : Math.floor(published);
      let candle = null;
      if (daily) {
        const eventDay = new Date(eventTime * 1000).toISOString().slice(0, 10);
        candle = chartData.find((item) => new Date(item.time * 1000).toISOString().slice(0, 10) === eventDay);
      }
      candle = candle || chartData.find((item) => item.time >= eventTime) || chartData.at(-1);
      if (!candle || eventTime < chartData[0].time - (daily ? 86_400 : 0)) continue;
      const current = grouped.get(candle.time) || { count: 0, titles: [], earliest: false };
      current.count += 1;
      if (event?.title) current.titles.push(event.title);
      current.earliest = current.earliest || event?.coverage_sequence === 'earliest_in_lookback';
      grouped.set(candle.time, current);
    }
    return [...grouped.entries()].map(([time, group]) => ({
      time,
      position: 'aboveBar',
      color: group.earliest ? '#2dd4bf' : '#facc15',
      shape: 'circle',
      text: group.count > 1
        ? `${group.earliest ? 'First news' : 'Follow-up'} x${group.count}`
        : group.earliest ? 'First news' : 'Follow-up',
    })).sort((a, b) => a.time - b.time);
  }, [chartData, daily, newsEvents]);

  useEffect(() => {
    if (!elRef.current || !hasData) return undefined;

    elRef.current.style.height = `${height}px`;

    const chart = createChart(elRef.current, {
      width: elRef.current.clientWidth,
      height,
      layout: {
        background: { type: 'solid', color: '#0b1226' },
        textColor: '#c7d2fe',
      },
      grid: {
        vertLines: { color: '#1a264a' },
        horzLines: { color: '#1a264a' },
      },
      rightPriceScale: {
        borderVisible: false,
      },
      timeScale: {
        borderVisible: false,
        timeVisible: true,
        secondsVisible: false,
      },
      crosshair: {
        mode: 1,
      },
      localization: {
        locale: navigator.language || 'en-US',
      },
    });

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#2dd4bf',
      downColor: '#f87171',
      borderUpColor: '#2dd4bf',
      borderDownColor: '#f87171',
      wickUpColor: '#2dd4bf',
      wickDownColor: '#f87171',
    });

    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: '',
    });

    volumeSeries.priceScale().applyOptions({
      scaleMargins: {
        top: 0.78,
        bottom: 0,
      },
    });

    chartRef.current = chart;
    candleSeriesRef.current = candleSeries;
    volumeSeriesRef.current = volumeSeries;
    newsMarkersRef.current = createSeriesMarkers(candleSeries, []);

    if (indicators) {
      const vwapSeries = chart.addSeries(LineSeries, {
        color: '#facc15',
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: true,
        title: daily ? 'VWAP' : 'Session VWAP',
      });
      const macdHistogram = chart.addSeries(HistogramSeries, {
        priceLineVisible: false,
        lastValueVisible: false,
        priceFormat: { type: 'price', precision: 3, minMove: 0.001 },
      }, 1);
      const macdSeries = chart.addSeries(LineSeries, {
        color: '#60a5fa',
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: false,
        title: 'MACD',
      }, 1);
      const signalSeries = chart.addSeries(LineSeries, {
        color: '#f472b6',
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        title: 'Signal',
      }, 1);

      indicatorSeriesRef.current = {
        vwap: vwapSeries,
        histogram: macdHistogram,
        macd: macdSeries,
        signal: signalSeries,
      };

      const panes = chart.panes();
      panes[0]?.setStretchFactor(3);
      panes[1]?.setStretchFactor(1);
    }

    const ro = new ResizeObserver(() => {
      if (!elRef.current) return;
      chart.applyOptions({
        width: elRef.current.clientWidth,
        height,
      });
    });

    ro.observe(elRef.current);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      candleSeriesRef.current = null;
      volumeSeriesRef.current = null;
      indicatorSeriesRef.current = {};
      newsMarkersRef.current = null;
      dataWindowRef.current = null;
    };
  }, [hasData, height, indicators, daily]);

  useEffect(() => {
    const chart = chartRef.current;
    const candleSeries = candleSeriesRef.current;
    const volumeSeries = volumeSeriesRef.current;
    if (!chart || !candleSeries || !volumeSeries || !chartData.length) return;

    candleSeries.setData(chartData);
    volumeSeries.setData(volumeData);
    const indicatorSeries = indicatorSeriesRef.current;
    indicatorSeries.vwap?.setData(vwapData);
    indicatorSeries.histogram?.setData(macdData.histogram);
    indicatorSeries.macd?.setData(macdData.macd);
    indicatorSeries.signal?.setData(macdData.signal);
    newsMarkersRef.current?.setMarkers(newsMarkers);

    const nextWindow = {
      firstTime: chartData[0].time,
      length: chartData.length,
    };
    const previousWindow = dataWindowRef.current;
    const historyWindowChanged = (
      !previousWindow
      || previousWindow.firstTime !== nextWindow.firstTime
      || Math.abs(previousWindow.length - nextWindow.length) > 1
    );
    if (historyWindowChanged) {
      if (chartData.length > visibleBars) {
        chart.timeScale().setVisibleLogicalRange({
          from: chartData.length - visibleBars,
          to: chartData.length + 5,
        });
      } else {
        chart.timeScale().fitContent();
      }
    }
    dataWindowRef.current = nextWindow;
  }, [chartData, volumeData, vwapData, macdData, newsMarkers, visibleBars]);

  if (chartData.length === 0) {
    return <div className="chart-empty">No candle data available.</div>;
  }

  return (
    <div className="candle-chart-shell">
      {indicators && (
        <div className="chart-indicator-legend" aria-label="Chart indicators">
          <span className="indicator-vwap">VWAP</span>
          <span className="indicator-macd">MACD</span>
          <span className="indicator-signal">Signal</span>
          <span>Volume</span>
          {!!newsMarkers.length && <span className="indicator-news">First news / follow-up</span>}
        </div>
      )}
      <div ref={elRef} className="candle-chart" />
    </div>
  );
}
