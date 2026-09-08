"""Paint assertions with the actual compiled app/Pico CSS, not DOM-only tests."""

def assert_state_node_paint(page):
    reports = []
    for theme in ('light', 'dark'):
        page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
        first = page.locator('g.goal').first
        first.scroll_into_view_if_needed()
        for interaction in ('normal', 'hover', 'focus'):
            if interaction == 'hover': first.hover()
            if interaction == 'focus': first.focus()
            rows = page.locator('g.goal').evaluate_all('''nodes => {
              const rgb = c => (c.match(/[\\d.]+/g) || []).slice(0,3).map(Number);
              const luma = color => rgb(color).map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4;}).reduce((a,v,i)=>a+v*[.2126,.7152,.0722][i],0);
              const ratio=(a,b)=>(Math.max(luma(a),luma(b))+.05)/(Math.min(luma(a),luma(b))+.05);
              return nodes.map(g=>{
                const card=g.querySelector('.card');
                if(!card) return {missingCard:true};
                const bg=getComputedStyle(card).fill;
                return {key:g.querySelector('.goal-key').textContent, width:card.getBoundingClientRect().width,
                  labels:[...g.querySelectorAll('text')].map(t=>{
                    const fill=getComputedStyle(t).fill;
                    const behind=t.classList.contains('goal-state')?getComputedStyle(g.querySelector('.status-background')).fill:bg;
                    const b=t.getBBox();
                    return {text:t.textContent,fill,background:behind,contrast:ratio(fill,behind),x:b.x,y:b.y,width:b.width,height:b.height};
                  })};
              });
            }''')
            for row in rows:
                assert not row.get('missingCard'), row
                assert row['width'] >= 260, ('unexpected SVG down-scaling', row)
                assert len(row['labels']) >= 5, row
                for label in row['labels']:
                    assert label['text'].strip(), ('empty painted label', row)
                    assert label['contrast'] >= 4.5, ('invisible/low contrast text', theme, interaction, row)
                    assert label['width'] > 0 and label['height'] > 0, row
                    assert 0 <= label['x'] and label['x']+label['width'] <= 265, ('clipped label', row)
            reports.append({'theme':theme,'interaction':interaction,'nodes':len(rows),
                            'minimum_contrast':min(x['contrast'] for row in rows for x in row['labels'])})
    page.evaluate("document.documentElement.dataset.theme = 'light'")
    return reports
