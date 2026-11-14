"use client";
import { useEffect, useState } from "react";
import BackendActivity from "@/components/BackendActivity";
import ConnectionMap from "@/components/ConnectionMap";
import FactoryCards from "@/components/FactoryCards";
import type { Machine } from "@/lib/types";

const initialMachines: Machine[] = [
  { machine_id:"machine_a", name:"Machine A", status:"healthy", records_synced:12408, backlog:0, fault_epoch:0, restart_count:0 },
  { machine_id:"machine_b", name:"Machine B", status:"healthy", records_synced:12384, backlog:0, fault_epoch:0, restart_count:0 },
  { machine_id:"machine_c", name:"Machine C", status:"healthy", records_synced:12396, backlog:0, fault_epoch:0, restart_count:0 },
];
function GhostMark(){return <svg viewBox="0 0 32 32" aria-hidden="true"><path d="M7 25V14a9 9 0 0 1 18 0v11l-3-2.2L19 25l-3-2.2L13 25l-3-2.2L7 25Z"/><circle cx="13" cy="14" r="1.3"/><circle cx="20" cy="14" r="1.3"/></svg>}

export default function Home(){
  const [machines,setMachines]=useState(initialMachines);
  const [runState,setRunState]=useState<"idle"|"faulted"|"running"|"complete">("idle"); const [activeStep,setActiveStep]=useState(-1);
  useEffect(()=>{const timer=window.setInterval(()=>setMachines(current=>current.map(machine=>machine.status==="failed"||machine.status==="recovering"?{...machine,backlog:machine.backlog+2}:{...machine,records_synced:machine.records_synced+4})),1200);return()=>window.clearInterval(timer)},[]);
  useEffect(()=>{if(runState!=="running"||activeStep<0)return;const timer=window.setTimeout(()=>{if(activeStep===3)setMachines(current=>current.map(m=>m.machine_id==="machine_b"?{...m,status:"recovering",restart_count:m.restart_count+1}:m));if(activeStep===5){setMachines(current=>current.map(m=>m.machine_id==="machine_b"?{...m,status:"reconciled",backlog:0}:m));setRunState("complete");return}setActiveStep(step=>step+1)},1100);return()=>window.clearTimeout(timer)},[activeStep,runState]);
  function injectFailure(){setMachines(current=>current.map(m=>m.machine_id==="machine_b"?{...m,status:"failed",fault_epoch:m.fault_epoch+1}:m));setRunState("faulted");setActiveStep(-1)}
  function runRecovery(){setRunState("running");setActiveStep(1)}
  function resetDemo(){setMachines(initialMachines);setRunState("idle");setActiveStep(-1)}
  const statusText=runState==="complete"?"Recovery verified":runState==="running"?"Recovery in progress":runState==="faulted"?"Action required":"All systems operational";
  return <main><header className="site-header"><a className="brand" href="#top"><span><GhostMark/></span>GhostShift</a><div className="header-meta"><span className="mock-badge">Mock backend</span><span className="live"><i/> Simulation running</span></div></header>
    <div className="shell" id="top"><section className="hero"><div><p className="kicker"><span/> Factory recovery</p><h1>Keep the shift<br/><em>moving.</em></h1></div><aside className="hero-proof"><p>System state</p><strong>{statusText}</strong><div className="proof-row"><span><i/> 3 machines</span><span>Live</span></div></aside></section>
      <section className="factory-section"><div className="section-heading"><div><p className="section-number">01 / FACTORY</p><h2>Machines</h2></div><span className="event-count"><i/> Connection preview</span></div><FactoryCards machines={machines}/><ConnectionMap machines={machines}/><div className="factory-actions" aria-label="Machine B simulation controls"><span>Machine B</span><button className="button danger" onClick={injectFailure} disabled={runState==="faulted"||runState==="running"}>Stop sync</button><button className="button primary" onClick={runRecovery} disabled={runState!=="faulted"}>Run recovery</button><button className="reset-button" onClick={resetDemo} disabled={runState==="running"}>Reset</button></div></section>
      <BackendActivity phase={runState} activeStep={activeStep}/>
      <footer><span>GhostShift / Toronto 2026</span><span>Agents in context ↗</span></footer></div></main>
}
