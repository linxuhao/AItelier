"""Live built-SPA checks against the browser suite's real isolated service."""
from playwright.sync_api import expect


def check_run_summary(page,service,sf,base,out,completed,paused):
    pid='run-summary-preview'
    service.create_project(pid,'Run summary preview')
    service.store.add_nodes(pid,[{'key':k,'goal':'Goal '+k,'acceptance':[{'id':'test','kind':'test','description':'Fixture check'}]} for k in ['one','two']])
    running=sf.create_run('browser_feature',project_id='summary-running');sf.start_run(running)
    failed=sf.create_run('browser_feature',project_id='summary-failed');sf.start_run(failed);sf.fail_run(failed,'Isolated test failure')
    for rid in [completed,paused,running,failed]:
        service.portfolio.add_reference(pid,'one','reference-'+rid,'run',rid,'Explicit test association','test-harness',protect=False)
    service.portfolio.add_reference(pid,'two','duplicate-running-reference','run',running,'Same run, another node','test-harness',protect=False)
    for rid,prompt,completion,hit,miss in [(completed,1000,200,600,400),(running,2000,300,800,1200),(failed,400,100,None,None)]:
        sf.trace(rid,'usage','token_usage',{'prompt_tokens':prompt,'completion_tokens':completion,'cache_hit_tokens':hit,'cache_miss_tokens':miss})
    events=service.store.events(pid)
    page.set_viewport_size({'width':1440,'height':1000})
    page.goto(base+'/#/state-projects/'+pid)
    panel=page.locator('.state-run-summary')
    expect(panel.locator('.running-runs a')).to_have_count(1)
    expect(panel.locator('[data-metric=total] dd')).to_have_text('4')
    expect(panel.locator('[data-metric=running] dd')).to_have_text('1')
    expect(panel.locator('[data-metric=finished] dd')).to_have_text('1')
    expect(panel.locator('[data-metric=failed] dd')).to_have_text('1')
    expect(panel.locator('[data-metric=tokens] dd')).to_have_text('4K*')
    expect(panel.locator('[data-metric=cache] dd')).to_have_text('46.7%')
    expect(panel.locator('[data-metric=cache] small')).to_contain_text('1.4K')
    assert panel.locator('.run-summary-metrics a,.run-summary-metrics button').count()==0
    for rid in [completed,paused,failed]:assert panel.locator(f'a[href="#/state-runs/{rid}"]').count()==0
    link=panel.locator('.running-runs a');expect(link).to_have_attribute('href','#/state-runs/'+running)
    page.screenshot(path=str(out/'run-summary-desktop.png'),full_page=True)
    panel.screenshot(path=str(out/'run-summary-panel.png'))
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+2'),'Summary overflows mobile'
    page.screenshot(path=str(out/'run-summary-mobile.png'),full_page=True)
    link.click()
    expect(page.locator('.state-run header code')).to_have_text(running)
    expect(page.locator('.graph-pin')).to_contain_text('graph v2')
    page.set_viewport_size({'width':1440,'height':1000})
    page.goto(base+'/#/state-projects/'+pid)
    expect(page.locator('.running-runs a')).to_have_count(1)
    sf.fail_run(running,'Fixture transitions while page is open')
    page.get_by_role('button',name='Reload view',exact=True).click()
    expect(page.locator('.state-run-summary .running-runs a')).to_have_count(0)
    expect(page.locator('[data-metric=running] dd')).to_have_text('0')
    expect(page.locator('[data-metric=failed] dd')).to_have_text('2')
    expect(page.locator('[data-metric=total] dd')).to_have_text('4')
    assert service.store.events(pid)==events,'A statistics read mutated State events'
    assert sf.get_run(paused)['status']=='paused'
    return {'project_id':pid,'related_runs':4,'running_before':1,'running_after':0,'tokens':4000,'cache_hit_ratio':1400/3000,
            'nonrunning_run_links':0,'nonclickable_metrics':6,'state_events_unchanged':True,'production_data_used':False}
