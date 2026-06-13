%% ═════════════════════════════════════════════════
%% 1. Static Architecture — who owns what
%% ═════════════════════════════════════════════════

graph TB
    classDef sf fill:#1a1a2e,stroke:#e94560,color:#eee,stroke-width:2px
    classDef ait fill:#0f3460,stroke:#53d8fb,color:#eee,stroke-width:2px
    classDef db fill:#16213e,stroke:#f5c518,color:#eee,stroke-width:2px
    classDef ext fill:#2d2d2d,stroke:#888,color:#ccc,stroke-width:1px

    subgraph DB["~/.AItelier/aitelier.db (shared SQLite)"]
        PROJ[skillflow_projects]
        GR[skillflow_graphs]
        RUN[skillflow_runs + project_id]
        ST[skillflow_steps]
        EC[skillflow_edge_counts]
        OB[skillflow_outbox]
        APROJ[projects]
        ATASK[tasks]
    end
    class DB db

    subgraph SF["skillflow (~/skillflow/src/skillflow/)"]
        Core["SkillFlow class<br/>create_run / advance_run<br/>claim_next_step / confirm_step<br/>pause / resume / reactivate"]
        Graph["GraphResolver<br/>validate / next_node<br/>resolve_transition<br/>is_gate / is_tool"]
        Tx["Transactional lock<br/>BEGIN IMMEDIATE<br/>optimistic versioning<br/>stale recovery"]
        Tools["ToolLoader<br/>read_file / write / repo_apply<br/>repo_validate / syntax_lint<br/>py_compile / pytest / notify"]
        Outbox["Outbox + NotificationBus<br/>drain / ack<br/>SSE bridge via host"]
        Projects["Project API (new)<br/>create_project / list_runs<br/>get_steps / get_or_create_run"]
        Schema["Schema + Migrations<br/>ALL_DDL<br/>SKILLFLOW_MIGRATIONS"]
    end
    class SF,Core,Graph,Tx,Tools,Outbox,Projects,Schema sf

    subgraph AIT["AItelier (~/AItelier/)"]
        TUI["CLI + TUI dashboard<br/>Textual / Rich<br/>chat / checkpoint modal"]
        API["FastAPI server<br/>projects + tasks CRUD<br/>checkpoint approve/reject<br/>SSE streaming"]
        Meta["Meta Agent<br/>intent detect<br/>brief conversation<br/>auto-submit"]
        Sched["Scheduler (APScheduler)<br/>poll_and_execute<br/>bridge skillflow ↔ aitelier"]
        Runner["AItelierStepRunner<br/>execute claimed step<br/>green / reviewer dispatch"]
        PE["PipelineEngine<br/>run_step (green LLM)<br/>run_review_step (red LLM)<br/>AgentFactory"]
        WS["Workspace Manager<br/>Inbox/Outbox dirs<br/>git event sourcing"]
        DI["api/dependencies.py<br/>singletons: SkillFlow<br/>ToolLoader / DBManager"]
    end
    class AIT,TUI,API,Meta,Sched,Runner,PE,WS,DI ait

    subgraph EXT["External"]
        LLM["LLM (DeepSeek / MiniMax<br/>via LiteLLM)"]
        Git["Git repos"]
    end
    class EXT,LLM,Git ext

    %% connections: AItelier → skillflow API
    Sched -->|"advance_run / claim / confirm"| Core
    Sched -->|"get_or_create_run"| Projects
    Sched -->|"get_steps / get_run"| Core
    API -->|"resume_run / reject_checkpoint"| Core

    %% connections: skillflow → DB (owns)
    Core --> RUN
    Core --> ST
    Core --> EC
    Projects --> PROJ
    Graph --> GR
    Outbox --> OB

    %% connections: AItelier → DB (owns its own)
    API --> APROJ
    API --> ATASK

    %% connections: AItelier internal
    TUI --> API
    API --> Meta
    Sched --> Runner --> PE
    PE --> WS
    DI -.-> Sched
    DI -.-> API

    %% connections: external
    PE --> LLM
    WS --> Git
    Outbox -.->|"SSE events"| API


%% ═════════════════════════════════════════════════
%% 2. Sequence — one scheduler tick
%% ═════════════════════════════════════════════════

sequenceDiagram
    participant Sch as Scheduler
    participant SF as SkillFlow
    participant DB as SQLite
    participant Run as AItelierStepRunner
    participant PE as PipelineEngine
    participant LLM

    Note over Sch,LLM: one tick = one step executed

    Sch->>SF: advance_run(run_id)
    SF->>DB: read last completed step + edge counts
    SF->>SF: resolve transition (flags → next node)
    SF->>DB: write current_node, auto-exec tools/gates
    SF-->>Sch: next step_id (or None if paused/done)

    alt step is task-level (t_plan/t_impl/t_verify)
        Sch->>DB: get_ready_tasks(project_id)
        DB-->>Sch: task_id
        Sch->>Sch: inject task_id into run_context
    end

    Sch->>SF: claim_next_step(run_id)
    SF->>DB: UPDATE step status pending→claimed (atomic)
    SF-->>Sch: ClaimedStep{token, step_id, step_config, inputs}

    Sch->>Run: execute(claimed)

    alt reviewer node (_review suffix)
        Run->>PE: run_review_step(reviewed_step_id)
        PE->>LLM: Red Agent call
        LLM-->>PE: {passed, feedback}
        PE-->>Run: verdict dict
    else green agent node
        Run->>PE: run_step(step_id)
        PE->>PE: AgentFactory.get_agent(name)
        PE->>LLM: Green Agent call (tool loop)
        LLM-->>PE: {files: {...}}
        PE->>PE: Gate validation + Red review
        PE-->>Run: StepOutput
    end

    Run->>Run: _compute_flags (has_tasks, more_tasks...)
    Run-->>Sch: StepResult{outputs, flags}

    Sch->>SF: confirm_step(token, result)
    SF->>DB: UPDATE step status→completed, store flags
    SF->>DB: set current_node=NULL

    Sch->>SF: advance_run(run_id)  — resolve next
    SF->>DB: read last completed step
    SF->>SF: resolve transition (flags match)
    alt checkpoint step
        SF->>DB: set run status=paused, current_node=next
    else normal step
        SF->>DB: set current_node=next
    end
