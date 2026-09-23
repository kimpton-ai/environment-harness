export type Json = null | boolean | number | string | Json[] | {[key: string]: Json};
export interface ResourceMetadata {id:string;createdAt:string;labels:Record<string,string>;}
export interface ResourceFeatures {required:string[];optional:string[];}
export interface LifecycleStatus {state:string;[key:string]:Json;}
export interface TrajectoryRecord {
  type:string;id:string;sequence:number;segment:string;participant:string|null;revision:number;
  causes:string[];time:{wallTime:string;native:{clock:string;value:Json}[]};data:Record<string,Json>;
  extensions:Record<string,Json>;
}
export interface TrajectorySegment {
  id:string;kind:string;sequenceStart:number;sequenceEnd:number;
  collection:LifecycleStatus;execution:LifecycleStatus;
}
export interface Trajectory {
  apiVersion:'environmentharness.dev/v1alpha1';kind:'Trajectory';metadata:ResourceMetadata;
  features:ResourceFeatures;spec:{manifest:Record<string,Json>};status:{segments:TrajectorySegment[];
    records:TrajectoryRecord[];collection:LifecycleStatus;execution:LifecycleStatus;
    termination:{terminated:boolean;truncated:boolean;reason:string};
    verifiedOutcome:{state:string;evidence:string[]};evidenceHead:string;trajectoryDigest:string};
  extensions:Record<string,Json>;
}
export interface TrajectorySnapshot {
  apiVersion:'environmentharness.dev/v1alpha1';kind:'TrajectorySnapshot';metadata:ResourceMetadata;
  features:ResourceFeatures;spec:Record<string,Json>;status:{recordCount:number;artifactCount:number;
    manifestDigest:string;recordsDigest:string;snapshotDigest:string;complete:boolean};extensions:Record<string,Json>;
}
export interface TrajectorySummary {id:string;trajectory_id:string;origin:'native'|'imported';collection_state:string;execution_state:string;namespace:string;run_id:string;}
export interface TrajectoryRecordPage {records:TrajectoryRecord[];cursor:number;has_more:boolean;}
export interface DatasetMember {trajectoryId:string;snapshotId:string;trajectoryDigest:string;snapshotDigest:string;schemaVersion:string;purpose:string;}
export interface TrajectoryDataset {
  apiVersion:'environmentharness.dev/v1alpha1';kind:'TrajectoryDataset';metadata:ResourceMetadata;
  features:ResourceFeatures;spec:{name:string;members:DatasetMember[]};
  status:{recordCount:number;datasetDigest:string;rewardState:string};extensions:Record<string,Json>;
}
export interface TrainingRun {
  apiVersion:'environmentharness.dev/v1alpha1';kind:'TrainingRun';metadata:ResourceMetadata;
  features:ResourceFeatures;spec:{datasetId:string;datasetDigest:string;integration:string;integrationVersion:string;configDigest:string};
  status:{state:string;policy:Record<string,Json>;metrics:Json;artifacts:Json[];limitations:string[];completedAt:string};extensions:Record<string,Json>;
}
export interface SourceRegistration {namespace:string;run_id:string;schema_version:string;environment:Record<string,Json>;participants:string[];purpose:'evaluation'|'training';split?:'training'|'heldout';}
export interface SourceRegistrationReceipt {id:string;namespace:string;run_id:string;registration_hash:string;collection_state:string;}
export interface SourceRecord {id:string;position:string;previous_hash:string;source_hash:string;type:string;segment:string;participant:string|null;revision:number;time:{wallTime:string;native:{clock:string;value:Json}[]};data:Record<string,Json>;audience:string[];}
export interface SourceAcknowledgement {source:string;accepted:number;position:string;hash:string;collection_state:string;}
export interface SourceStatus {source:string;namespace:string;run_id:string;registration_hash:string;collection_state:string;execution_state:string;acknowledged_position:string|null;acknowledged_hash:string|null;backlog:number|null;gaps:string[];capture_failures:string[];termination:{terminated:boolean;truncated:boolean;reason:string};verified_outcome:{state:string;evidence:string[]};}
export interface SourceStatusUpdate {collection_state:string;execution_state:string;termination:{terminated:boolean;truncated:boolean;reason:string};verified_outcome:{state:string;evidence:string[]};terminal_position:string;terminal_hash:string;backlog:number|null;gaps:string[];capture_failures:string[];}
export type CommandOperation = 'advance'|'lease'|'release'|'cancel'|'resolve'|'close_phase'|'checkpoint'|'reconcile_agent'|'resume'|'branch'|'control'|'memory'|'transfer'|'external_event'|'finalize_outcomes';
export interface AdvanceResponse {status:string;revision:number;deadline_exceeded?:boolean;event?:number;}
export interface Scenario<Input extends Json = Json> {id:string;input:Input;reference:Json;metadata:Record<string,Json>;}
export interface EvidenceEvent {environment: string; seq: number; revision: number; kind: string; payload: Record<string, Json>; audience: string[]; event_time: number | null; ingested: number; previous: string; hash: string;}
export interface Environment {id: string; revision: number; status: string; participants: string[]; lineage: string; parent: string | null; checkpoint: string | null; spent_micros: number; reserved_micros: number; environment: Record<string, Json>; experiment?: Record<string, Json>; scheduler?: Record<string, Json>;}
export interface Action {operation_id: string; participant: string; observation_id: string; revision: number; payload: Record<string, Json>;}
export interface Observation {id: string; environment: string; participant: string; generation: number; revision: number; payload: Record<string, Json>; memory: Record<string, Json>; may_act: boolean; deadline: number;}
export interface Lease {owner: string; epoch: number; expires: number;}
export interface Checkpoint {id: string; revision: number; hash: string; exact_agents: boolean;}
export interface Capabilities {replay: boolean; checkpoint: boolean; resume: boolean; branch: boolean; agent_checkpoint: boolean; rendered_requests: boolean; token_ids: boolean; logprobs: boolean; live_reads: boolean; external_writes: boolean;}
export interface OperationSpec {name:string; version:string; config:Record<string,Json>;}
export interface EnvironmentSpec {protocol: 'environment-session.v1'; id: string; version: string; implementation: string; observation_schema: Record<string,Json>; action_schema: Record<string,Json>; scenario_schema: Record<string,Json>; scheduling: 'sequential'|'simultaneous'|'event'; modalities: string[]; operations:OperationSpec[]; capabilities: Capabilities; purposes: ('evaluation'|'training')[]; stale_action: 'reject'; missing_action: 'reject'|'noop'; phase_seconds: number; phase_deadline: 'wall'|'coordinator';}
export interface AgentSpec {id:string; implementation:string; policy_version:string; config:Record<string,Json>; checkpoint:boolean;}
export interface RunPolicy {max_turns:number; max_cost_micros:number; max_event_bytes:number; max_artifact_bytes:number; max_state_bytes:number; allowed_endpoints:string[]; allowed_operations:string[]; external_writes:boolean;}
export interface ExperimentSpec {environment:EnvironmentSpec; participants:AgentSpec[]; seed:number; scenario:string; scenario_input:Json; scenario_reference:Json; scenario_metadata:Record<string,Json>; split:'training'|'heldout'; purpose:'evaluation'|'training'; time_boundary:string; interventions:Record<string,Json>; scoring_versions:string[]; policy:RunPolicy; operations:OperationSpec[];}
export interface Finding {rule:string; participant:string; observation_id:string; action_id:string|null; action_item?:string|null; opportunity_event?:number|null; outcome_event:number; consequence_events:number[]; category:'competence'|'compliance'|'harm'|'infrastructure'|'malformed'; status:'attempted'|'blocked'|'executed'|'consequential'|'omitted'|'inconclusive'; judgment:string; uncertainty:string;}
export interface ScoreReport {scorer:string; version:string; kind:'deterministic'|'model'|'human'; evidence_cursor:number; metrics:Record<string,Json>; metric_definitions?:Record<string,MetricDefinition>; findings:Finding[]; rewards:Record<string,number>; uncertainty:string; provenance:Record<string,Json>;}
export interface MetricDefinition {id: string; version: string; unit: string;}
export interface ReportEnvelope {environment: string; revision: number; report: ScoreReport; hash: string;}
export interface ActivityEvent {id:number; topic:string; experiment:string|null; environment:string|null; kind:string; payload:Record<string,Json>; created:number;}
export interface ActivitySession {kind:'session'; id:string; scenario_id:string; trial:number; status:string; current_turn:number; target_turns:number|null; participants:string[]; latest_activity:string|null; failure:string|null; environment:Record<string,Json>; frozen:Record<string,Json>; updated:number;}
export interface ActivityScenario {kind:'scenario'; id:string; input:Json; reference:Json; metadata:Record<string,Json>; status:string; completed:number; total:number; running:number; queued:number; failed:number; latest_activity:string|null; sessions:ActivitySession[]; updated:number;}
export interface ActivityExperiment {kind:'experiment'; id:string; name:string; status:string; progress:{completed:number;total:number}; running:number; queued:number; failed:number; latest_activity:string|null; score_summary:Record<string,number>; frozen:Record<string,Json>; scenarios:ActivityScenario[]; sessions:ActivitySession[]; updated:number;}
export interface ActivitySnapshot {summary:{running:number;queued:number;failed:number}; experiments:ActivityExperiment[]; standalone:ActivitySession[]; cursor:number;}
export interface ActivityPage {events:ActivityEvent[];cursor:number;}
export interface TurnSeriesPoint {turn:number;revision:number;value:number;}
export interface TurnSeriesRecord {
  id:string;label:string;kind:'reward'|'signal'|'activity';unit:string|null;participant:string|null;
  source_points:number;downsampled:boolean;points:TurnSeriesPoint[];
}
export interface TurnSeriesResponse {
  environment:string;total_turns:number;range:{start_turn:number;end_turn:number};max_points:number;
  series:TurnSeriesRecord[];
}
export interface MetricSummary {mean_of_lineage_means: number; independent_lineages: number; standard_error: number | null;}
export interface MetricGroup {
  id: string; cohort: string; scorer: string; version: string; kind: ScoreReport['kind']; metric: string;
  definition: MetricDefinition | null; experiment: Record<string, Json>;
  values: {environment: string; lineage: string; status: string; report_revision: number; report_hash: string; value: number}[];
  summary: MetricSummary | null; selected_environments: number; reported_environments: number;
  missing_environments: number; incomplete_environments: number;
}
export interface Comparison {
  environments: {environment: string; lineage: string; parent: string | null; participants: string[]; status: string; revision: number;
    cost_micros: number; interventions: Record<string, Json>; latest_report: ReportEnvelope | null; selected_reports: ReportEnvelope[]}[];
  metrics: Record<string, MetricSummary>; metric_groups: MetricGroup[]; warnings: string[]; uncertainty: string; design: string;
}
