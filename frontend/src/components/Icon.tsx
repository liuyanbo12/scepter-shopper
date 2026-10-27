type Name = 'cursor' | 'external' | 'arrow' | 'list' | 'check' | 'pause' | 'play' | 'stop' | 'refresh' | 'clock';
const paths: Record<Name, string> = {
  cursor: 'M5 3l15 13-8 1-3 7L5 3z', external: 'M14 3h7v7M21 3l-9 9M10 5H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-5',
  arrow: 'M4 12h16M14 6l6 6-6 6', list: 'M8 6h12M8 12h12M8 18h12M3 6h.01M3 12h.01M3 18h.01',
  check: 'M5 12l4 4L19 6', pause: 'M8 5v14M16 5v14', play: 'M7 4l13 8-13 8V4z', stop: 'M5 5h14v14H5z',
  refresh: 'M20 7a8 8 0 1 0 0 10M20 3v5h-5', clock: 'M12 8v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',
};
export function Icon({ name, size = 18 }: { name: Name; size?: number }) { return <svg width={size} height={size} viewBox="0 0 24 24" fill={name === 'cursor' ? 'currentColor' : 'none'} stroke={name === 'cursor' ? 'none' : 'currentColor'} strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name]} /></svg>; }
