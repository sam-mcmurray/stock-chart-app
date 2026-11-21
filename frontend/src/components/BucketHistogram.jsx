// src/components/BucketHistogram.jsx
import { useEffect, useMemo, useRef } from 'react';
import { createChart, HistogramSeries } from 'lightweight-charts';

export default function BucketHistogram({ data = [], height = 260 }) {
  const elRef = useRef(null);
  const chartRef = useRef(null);
  const seriesRef = useRef(null);

  // Map your {bucket, pl} to histogram points with a synthetic time axis
  const { points, labelMap } = useMemo(() => {
    const safeData = Array.isArray(data) ? data : [];
    const base = Math.floor(Date.now() / 1000) - (safeData.length + 2) * 86400;
    const lm = {};

    const pts = safeData.map((b, i) => {
      const t = base + (i + 1) * 86400;
      lm[t] = b.bucket;
    
      return {
        time: t,
        value: Number(b.pl || 0),
        color: (b.pl || 0) >= 0 ? '#2dd4bf' : '#f87171',
      };
    });

    return { points: pts, labelMap: lm };
  }, [data]);

  useEffect(() => {
    if (!elRef.current) return;

    elRef.current.style.height = `${height}px`;

    const chart = createChart(elRef.current, {
      width: elRef.current.clientWidth,
      height,
      layout: {
        background: { type: 'solid', color: '#0b1226' },
        textColor: '#c7d2fe',
      },
      grid: {
        vertLines: { color: '#1a264a', style: 0, visible: true },
        horzLines: { color: '#1a264a', style: 0, visible: true },
      },
      rightPriceScale: {
        borderVisible: false,
      },
      timeScale: {
        borderVisible: false,
        secondsVisible: false,
        timeVisible: false,
        tickMarkFormatter: (time) => labelMap[time] ?? '',
      },
      crosshair: {
        mode: 1,
      },
      localization: {
        locale: navigator.language || 'en-US',
      },
    });

    const series = chart.addSeries(HistogramSeries, {
      priceFormat: {
        type: 'price',
        precision: 2,
        minMove: 0.01,
      },
      base: 0,
    });

    series.setData(points);
    chart.timeScale().fitContent();

    const ro = new ResizeObserver(() => {
      if (!elRef.current) return;

      chart.applyOptions({
        width: elRef.current.clientWidth,
        height,
      });

      chart.timeScale().fitContent();
    });

    ro.observe(elRef.current);

    chartRef.current = chart;
    seriesRef.current = series;

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, [points, labelMap, height]);

  return <div ref={elRef} className="bucket-histogram" />;
}