export type Json = null | boolean | number | string | Json[] | {[key: string]: Json};
export interface EvidenceEvent {environment: string; seq: number; revision: number; kind: string; payload: Record<string, Json>; audience: string[]; event_time: number | null; ingested: number; previous: string; hash: string;}
export interface Environment {id: string; revision: number; status: string; participants: string[]; lineage: string; parent: string | null; checkpoint: string | null; spent_micros: number; reserved_micros: number; environment: Record<string, Json>; experiment?: Record<string, Json>; scheduler?: Record<string, Json>;}
export interface Action {operation_id: string; participant: string; observation_id: string; revision: number; payload: Record<string, Json>;}
export interface Observation {id: string; environment: string; participant: string; generation: number; revision: number; payload: Record<string, Json>; memory: Record<string, Json>; may_act: boolean; deadline: number;}
export interface Lease {owner: string; epoch: number; expires: number;}
export interface Checkpoint {id: string; revision: number; hash: string; exact_agents: boolean;}
export interface Capabilities {replay: boolean; checkpoint: boolean; resume: boolean; branch: boolean; agent_checkpoint: boolean; rendered_requests: boolean; token_ids: boolean; logprobs: boolean; live_reads: boolean; external_writes: boolean;}
export interface EnvironmentSpec {protocol: 'environment-session.v1'; id: string; version: string; implementation: string; observation_schema: Record<string,Json>; action_schema: Record<string,Json>; scheduling: 'sequential'|'simultaneous'|'event'; modalities: string[]; capabilities: Capabilities; purposes: ('evaluation'|'training')[]; stale_action: 'reject'; missing_action: 'reject'|'noop'; phase_seconds: number; phase_deadline: 'wall'|'coordinator'; motor_skills?: string[];}
export interface AgentSpec {id:string; implementation:string; policy_version:string; config:Record<string,Json>; checkpoint:boolean;}
export interface RunPolicy {max_turns:number; max_cost_micros:number; max_event_bytes:number; max_artifact_bytes:number; max_state_bytes:number; allowed_endpoints:string[]; allowed_operations:string[]; external_writes:boolean;}
export interface ExperimentSpec {environment:EnvironmentSpec; participants:AgentSpec[]; seed:number; scenario:string; split:'training'|'heldout'; purpose:'evaluation'|'training'; time_boundary:string; interventions:Record<string,Json>; scoring_versions:string[]; policy:RunPolicy; motor?:MotorProfile|null;}
export interface MotorProfile {mode?: 'deterministic'|'jev'; executor?: 'motor.v1'; adapter:string; selector_model?: string|null;}
export interface MotorRequest {skill:string; target:Record<string,Json>; arguments?:Record<string,Json>; expected:Record<string,Json>; observation_revision:string; goal_revision:string; stop_epoch?:number; max_steps?:number; timeout_ms?:number;}
export interface MotorStep {operation:string; target:Record<string,Json>; arguments?:Record<string,Json>;}
export interface MotorCandidate {id:string; description:string; steps:MotorStep[];}
export interface MotorSelection {candidate_id?:string|null; model:string; cost_micros?:number; usage?:Record<string,Json>; probabilities?:Record<string,number>; confidence?:number|null;}
export interface MotorReceipt {operation_id:string; status:'completed'|'blocked'|'cancelled'; cost_micros?:number; profile:MotorProfile; request:MotorRequest; reason?:string|null; before?:Record<string,Json>; after?:Record<string,Json>; selection?:MotorSelection|null; steps?:Record<string,Json>[]; elapsed_ms:number;}
export interface MotorAdapter {implementation:string; skills:string[]; observe():Record<string,Json>; plan(request:MotorRequest, observation:Record<string,Json>):MotorCandidate[]; execute(step:MotorStep, operation_id:string, cancel?:AbortSignal, deadline?:number):Record<string,Json>; stop():void; lookup(operation_id:string):Record<string,Json>|null;}
export interface MotorExecutor {acknowledge_unknown():{operation_ids:string[]; observation:Record<string,Json>; stop_epoch:number}; progress(operation_id:string):{status:string; steps:Record<string,Json>[]} | null; execute(operation_id:string, request:Record<string,Json>, maximum_cost_micros:number):MotorReceipt; stop():void; lookup(operation_id:string):MotorReceipt|null;}
export interface Finding {rule:string; participant:string; observation_id:string; action_id:string|null; action_item?:string|null; opportunity_event?:number|null; outcome_event:number; consequence_events:number[]; category:'competence'|'compliance'|'harm'|'infrastructure'|'malformed'; status:'attempted'|'blocked'|'executed'|'consequential'|'omitted'|'inconclusive'; judgment:string; uncertainty:string;}
export interface ScoreReport {scorer:string; version:string; kind:'deterministic'|'model'|'human'; evidence_cursor:number; metrics:Record<string,Json>; metric_definitions?:Record<string,MetricDefinition>; findings:Finding[]; rewards:Record<string,number>; uncertainty:string; provenance:Record<string,Json>;}
export interface MetricDefinition {id: string; version: string; unit: string;}
export interface ReportEnvelope {environment: string; revision: number; report: ScoreReport; hash: string;}
export interface MetricSummary {mean_of_lineage_means: number; independent_lineages: number; standard_error: number | null;}
export interface MetricGroup {
  id: string; cohort: string; scorer: string; version: string; kind: ScoreReport['kind']; metric: string;
  definition: MetricDefinition | null; experiment: Record<string, Json>;
  values: {environment: string; lineage: string; status: string; report_revision: number; report_hash: string; value: number}[];
  summary: MetricSummary | null; selected_environments: number; reported_environments: number;
  missing_environments: number; incomplete_environments: number;
}
export interface Comparison {
  environments: {environment: string; parent: string | null; participants: string[]; status: string; revision: number;
    cost_micros: number; interventions: Record<string, Json>; latest_report: ReportEnvelope | null; selected_reports: ReportEnvelope[]}[];
  metrics: Record<string, MetricSummary>; metric_groups: MetricGroup[]; warnings: string[]; uncertainty: string; design: string;
}
