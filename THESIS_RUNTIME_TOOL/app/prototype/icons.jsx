/* Shared line icons — 16px grid, stroke 1.5, technical/minimal */
const Ic = {};
function mk(paths, opts = {}) {
  return function Icon({ size = 14, className = "", style = {} }) {
    return (
      <svg width={size} height={size} viewBox="0 0 16 16" fill="none"
        stroke="currentColor" strokeWidth={opts.w || 1.5}
        strokeLinecap="round" strokeLinejoin="round"
        className={className} style={style} aria-hidden="true">
        {paths}
      </svg>
    );
  };
}

Ic.chevDown = mk(<path d="M4 6l4 4 4-4" />);
Ic.chevRight = mk(<path d="M6 4l4 4-4 4" />);
Ic.chevUpDown = mk(<><path d="M5 6.5L8 3.5l3 3" /><path d="M5 9.5l3 3 3-3" /></>);
Ic.check = mk(<path d="M3.5 8.5l3 3 6-6.5" />, { w: 1.75 });
Ic.checkSmall = mk(<path d="M3.5 8l3 3 6-6.5" />, { w: 1.75 });
Ic.dot = mk(<circle cx="8" cy="8" r="2.4" fill="currentColor" stroke="none" />);
Ic.lock = mk(<><rect x="3.5" y="7" width="9" height="6" rx="1.2" /><path d="M5.5 7V5.2a2.5 2.5 0 0 1 5 0V7" /></>);
Ic.folder = mk(<path d="M2 4.5A1 1 0 0 1 3 3.5h3l1.2 1.4H13a1 1 0 0 1 1 1V12a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1z" />);
Ic.file = mk(<><path d="M4 2.5h5l3 3V13a.5.5 0 0 1-.5.5h-7A.5.5 0 0 1 4 13z" /><path d="M9 2.5V5.5h3" /></>);
Ic.save = mk(<><path d="M3 3.5h7.5L13 6v6.5a.5.5 0 0 1-.5.5h-9a.5.5 0 0 1-.5-.5z" /><path d="M5.5 3.5v3h4v-3" /><path d="M5.5 13v-3.5h5V13" /></>);
Ic.upload = mk(<><path d="M8 10.5V3.5" /><path d="M5 6l3-3 3 3" /><path d="M3 11.5v1a.5.5 0 0 0 .5.5h9a.5.5 0 0 0 .5-.5v-1" /></>);
Ic.play = mk(<path d="M5 3.5l7 4.5-7 4.5z" />);
Ic.shield = mk(<path d="M8 2.5l4.5 1.6v3.4c0 3-2 4.7-4.5 5.9C5.5 12.2 3.5 10.5 3.5 7.5V4.1z" />);
Ic.snow = mk(<><path d="M8 2.5v11M3.2 5.2l9.6 5.6M12.8 5.2L3.2 10.8" /></>, { w: 1.3 });
Ic.bolt = mk(<path d="M9 2.5L4.5 9H8l-1 4.5L11.5 7H8z" />);
Ic.flag = mk(<><path d="M4 13.5V3" /><path d="M4 3.5h7l-1.4 2.3L11 8H4" /></>);
Ic.book = mk(<><path d="M8 4.2C7 3.3 5.4 3 3.5 3.2v8.2C5.4 11.2 7 11.5 8 12.4" /><path d="M8 4.2c1-1 2.6-1.2 4.5-1v8.2c-1.9-.2-3.5.1-4.5 1" /></>);
Ic.tag = mk(<><path d="M3 3.5h4.2l5.3 5.3a1 1 0 0 1 0 1.4l-2.3 2.3a1 1 0 0 1-1.4 0L3.5 7.2V3.5z" /><circle cx="5.6" cy="5.6" r=".7" fill="currentColor" stroke="none" /></>);
Ic.users = mk(<><circle cx="6" cy="6" r="2.2" /><path d="M2.5 13c0-2 1.6-3.2 3.5-3.2S9.5 11 9.5 13" /><path d="M10.5 4.2a2.2 2.2 0 0 1 0 3.6M11 9.9c1.5.3 2.5 1.5 2.5 3.1" /></>);
Ic.doc = mk(<><path d="M4 2.5h5l3 3V13a.5.5 0 0 1-.5.5h-7A.5.5 0 0 1 4 13z" /><path d="M6 7.5h4M6 9.5h4M6 11.5h2.5" /></>);
Ic.list = mk(<><path d="M3 4.5h1M3 8h1M3 11.5h1" /><path d="M6 4.5h7M6 8h7M6 11.5h7" /></>, { w: 1.3 });
Ic.checkCircle = mk(<><circle cx="8" cy="8" r="5.5" /><path d="M5.5 8l1.8 1.8 3.2-3.6" /></>);
Ic.alert = mk(<><path d="M8 2.8L14 13H2z" /><path d="M8 6.5v3M8 11.2v.1" /></>);
Ic.xCircle = mk(<><circle cx="8" cy="8" r="5.5" /><path d="M6 6l4 4M10 6l-4 4" /></>);
Ic.x = mk(<path d="M3.5 3.5l9 9M12.5 3.5l-9 9" />);
Ic.plus = mk(<path d="M8 3.5v9M3.5 8h9" />);
Ic.trash = mk(<><path d="M3.5 4.5h9M6 4.5V3.2h4v1.3M5 4.5l.5 8h5l.5-8" /></>, { w: 1.3 });
Ic.search = mk(<><circle cx="7" cy="7" r="3.8" /><path d="M10 10l3 3" /></>);
Ic.arrowRight = mk(<path d="M3 8h9M9 5l3 3-3 3" />);
Ic.pencil = mk(<path d="M10.5 3l2.5 2.5L6 12.5 3 13l.5-3z" />);
Ic.clock = mk(<><circle cx="8" cy="8" r="5.5" /><path d="M8 5v3l2 1.5" /></>);
Ic.eye = mk(<><path d="M1.5 8S4 3.5 8 3.5 14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8z" /><circle cx="8" cy="8" r="1.8" /></>);
Ic.layers = mk(<><path d="M8 2.5l5.5 3-5.5 3-5.5-3z" /><path d="M2.5 8.5L8 11.5l5.5-3" /></>);
Ic.filter = mk(<path d="M2.5 4h11l-4.2 5v3.5L6.7 14V9z" />);
Ic.quote = mk(<><path d="M4 9.5C4 7 5.5 5.5 7 5.5M4 9.5h2.5v-2.5" /><path d="M9.5 9.5C9.5 7 11 5.5 12.5 5.5M9.5 9.5H12v-2.5" /></>, { w: 1.3 });
Ic.sparkle = mk(<path d="M8 2.5l1.3 3.4L12.5 7 9.3 8.1 8 11.5 6.7 8.1 3.5 7l3.2-1.1z" />);
Ic.refresh = mk(<><path d="M12.5 6.5A4.5 4.5 0 1 0 13 9.5" /><path d="M12.5 3v3.5H9" /></>);
Ic.undo = mk(<><path d="M6.5 4L3.5 7l3 3" /><path d="M4 7h5.5A3.5 3.5 0 1 1 6 10.5" /></>);
Ic.redo = mk(<><path d="M9.5 4l3 3-3 3" /><path d="M12 7H6.5A3.5 3.5 0 1 0 10 10.5" /></>);

window.Ic = Ic;
