import { ServerNode, Endpoint } from '@matter/main';
import { AggregatorEndpoint } from '@matter/main/endpoints/aggregator';
import { LaundryWasherDevice } from '@matter/main/devices/laundry-washer';
import { RoomAirConditionerDevice } from '@matter/main/devices/room-air-conditioner';
import { ColorTemperatureLightDevice } from '@matter/main/devices/color-temperature-light';
import { BridgedDeviceBasicInformationServer } from '@matter/main/behaviors/bridged-device-basic-information';
import { OperationalStateServer } from '@matter/main/behaviors/operational-state';
import { OnOffServer } from '@matter/main/behaviors/on-off';
import { ThermostatServer } from '@matter/main/behaviors/thermostat';
import { LevelControlServer } from '@matter/main/behaviors/level-control';
import { ColorControlServer } from '@matter/main/behaviors/color-control';
import { hasLocalActor } from '@matter/protocol';
import { inflateRawSync } from 'node:zlib';
import { mkdirSync, existsSync, readFileSync, writeFileSync, renameSync } from 'node:fs';
import { join } from 'node:path';
import { isDeepStrictEqual } from 'node:util';
import mqtt from 'mqtt';
import { HemsBase, root, emit, port, passcode, discriminator, basicInformation } from './common.mjs';

mkdirSync(root, { recursive: true });
const journalPath = join(root, 'bridge-commands.json');
const journal = existsSync(journalPath) ? JSON.parse(readFileSync(journalPath, 'utf8')) : {};
const saveJournal = () => { writeFileSync(journalPath + '.tmp', JSON.stringify(journal)); renameSync(journalPath + '.tmp', journalPath); };
const prefix = (process.env.HEMS_MQTT_ROOT || 'home/hems').replace(/\/$/, '');
const mqttClient = mqtt.connect({ host: process.env.MQTT_HOST || '127.0.0.1', port: Number(process.env.MQTT_PORT || 1883),
  reconnectPeriod: 1000, clean: true, queueQoSZero: false });
const endpoints = new Map(), contexts = new Map(), chunks = new Map(), pending = new Map(), snapshots = new Map();
const deviceFor = endpoint => endpoint.id;

class HemsServer extends HemsBase {
  prepare({ id, index, count, chunk }) {
    const device = deviceFor(this.endpoint);
    if (!count || count > 256 || index >= count || chunk.length > 600) throw new Error('Invalid context chunk');
    let transfer = chunks.get(device);
    if (!transfer || transfer.id !== id || transfer.count !== count || index === 0) {
      transfer = { id, count, parts: Array(count), at: Date.now() };
      chunks.set(device, transfer);
    }
    if (Date.now() - transfer.at > 5000) throw new Error('Context transfer expired');
    transfer.parts[index] = chunk;
    if (transfer.parts.filter(part => part !== undefined).length === count) {
      const command = JSON.parse(inflateRawSync(Buffer.from(transfer.parts.join(''), 'base64'), { maxOutputLength: 65536 }).toString('utf8'));
      if (command.command_id !== id || command.device_id !== device || Date.parse(command.expires_at) <= Date.now()) throw new Error('Invalid/expired execution context');
      if (!['query', 'reset', 'start', 'stop', 'set'].includes(command.action)) throw new Error('Invalid device action');
      if (command.action === 'set' && (device !== 'bulb-1' || command.protocol_version !== 2 ||
          !command.authorization_ref?.startsWith('request:') || command.expected_state_version < 0 ||
          (command.level !== null && (!Number.isInteger(command.level) || command.level < 1 || command.level > 254)) ||
          (command.mired !== null && (!Number.isInteger(command.mired) || command.mired < 154 || command.mired > 370)))) throw new Error('Invalid bulb context');
      if (command.action === 'start' && (!command.plan || !command.approval || !command.task_id || !command.run_id || command.plan_version < 1)) throw new Error('Missing approval');
      const previous = journal[id];
      if (previous && !isDeepStrictEqual(previous.command, command)) throw new Error('Command ID reused with different context');
      contexts.set(device, command);
      chunks.delete(device);
      if (!previous) {
        journal[id] = { command, prepared: true };
        saveJournal();
      }
    }
  }
  async auxiliary() {
    const device = deviceFor(this.endpoint), action = contexts.get(device)?.action;
    if (!['query', 'reset'].includes(action)) throw new Error('Auxiliary action requires fresh context');
    const result = await execute(device, action);
    this.state.result = JSON.stringify(result);
  }
}

async function execute(device, action) {
  const command = contexts.get(device);
  if (!command || command.action !== action || Date.parse(command.expires_at) <= Date.now()) throw new Error('No matching fresh approval context');
  if (!mqttClient.connected) throw new Error('MQTT device link offline');
  const previous = journal[command.command_id];
  if (previous?.result) {
    if (!previous.result.accepted) throw new Error(previous.result.message);
    return previous.result;
  }
  const snapshot = snapshots.get(device);
  if (!snapshot || Date.now() - Date.parse(snapshot.observed_at) > 10000 || snapshot.boot_id !== command.expected_boot ||
      (device === 'bulb-1' ? snapshot.state_version !== command.expected_state_version : snapshot.clock_id !== command.clock_id)) throw new Error('Device state is stale or identity changed');
  // Device also persists deduplication. No retry with a different command ID.
  const result = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => { pending.delete(command.command_id); reject(new Error('MQTT confirmation timeout')); }, 4000);
    pending.set(command.command_id, value => { clearTimeout(timeout); resolve(value); });
    mqttClient.publish(`${prefix}/devices/${device}/command`, JSON.stringify(command), { qos: 1, retain: false }, error => {
      if (error) { clearTimeout(timeout); pending.delete(command.command_id); reject(error); }
    });
  });
  journal[command.command_id] = { command, result };
  saveJournal();
  if (!result.accepted) throw new Error(result.message);
  return result;
}

class WasherOperations extends OperationalStateServer {
  async start() { this.agent.get(HemsServer).state.result = JSON.stringify(await execute(deviceFor(this.endpoint), 'start')); return { commandResponseState: { errorStateId: 0 } }; }
  async stop() { this.agent.get(HemsServer).state.result = JSON.stringify(await execute(deviceFor(this.endpoint), 'stop')); return { commandResponseState: { errorStateId: 0 } }; }
  async pause() { throw new Error('Pause is not authorized by this teaching program'); }
  async resume() { throw new Error('Resume is not supported'); }
}
class ACOnOff extends OnOffServer {
  async on() { this.agent.get(HemsServer).state.result = JSON.stringify(await execute(deviceFor(this.endpoint), 'start')); }
  async off() { this.agent.get(HemsServer).state.result = JSON.stringify(await execute(deviceFor(this.endpoint), 'stop')); }
  async toggle() { throw new Error('Explicit approved action required'); }
}
class ACThermostat extends ThermostatServer.with('Cooling') {
  async initialize() {
    await super.initialize();
    this.reactTo(this.events.occupiedCoolingSetpoint$Changing, value => {
      if (hasLocalActor(this.context)) return;
      // Local telemetry updates don't command the physical model. Remote setpoint
      // writes require the same prepared start envelope as OnOff.on.
      const c = contexts.get(deviceFor(this.endpoint));
      if (value !== this.state.occupiedCoolingSetpoint && (!c?.plan || c.action !== 'start' || Date.parse(c.expires_at) <= Date.now() || value !== Math.round(c.plan.constraints.target_c * 100))) throw new Error('Setpoint outside prepared plan');
    });
  }
}
function bulbContext(expectedField, expectedValue) {
  const command = contexts.get('bulb-1');
  if (!command || command.action !== 'set' || command[expectedField] !== expectedValue) throw new Error('Bulb operation outside prepared target');
  return command;
}
async function bulbExecute(expectedField, expectedValue) {
  bulbContext(expectedField, expectedValue);
  return execute('bulb-1', 'set');
}
class BulbOnOff extends OnOffServer.with('Lighting') {
  async on() { this.agent.get(HemsServer).state.result = JSON.stringify(await bulbExecute('power', true)); }
  async off() { this.agent.get(HemsServer).state.result = JSON.stringify(await bulbExecute('power', false)); }
  async toggle() { throw new Error('Explicit bulb target required'); }
}
class BulbLevel extends LevelControlServer.with('OnOff', 'Lighting') {
  async moveToLevelWithOnOff({ level }) { this.agent.get(HemsServer).state.result = JSON.stringify(await bulbExecute('level', level)); }
  async moveToLevel({ level }) { this.agent.get(HemsServer).state.result = JSON.stringify(await bulbExecute('level', level)); }
}
class BulbColor extends ColorControlServer.with('ColorTemperature') {
  async moveToColorTemperature({ colorTemperatureMireds }) {
    const command = bulbContext('mired', colorTemperatureMireds);
    // Combined targets are dispatched by their final Level/OnOff operation.
    // A color-only target is complete at this command and executes here.
    if (command.level === null && command.power === null)
      this.agent.get(HemsServer).state.result = JSON.stringify(await execute('bulb-1', 'set'));
  }
}

const node = await ServerNode.create({ id: 'hems-bridge', network: { port },
  commissioning: { passcode, discriminator }, basicInformation: basicInformation('HEMS bridge') });
const aggregator = await node.add(new Endpoint(AggregatorEndpoint, { id: 'appliances', number: 1 }));
const washer = await aggregator.add(new Endpoint(LaundryWasherDevice.with(WasherOperations, HemsServer, BridgedDeviceBasicInformationServer), {
  id: 'washer-1', number: 2, bridgedDeviceBasicInformation: { nodeLabel: 'Virtual washer', uniqueId: 'hems-washer-1', reachable: false },
  hemsTask: { snapshot: '', result: '', clusterRevision: 1 },
  operationalState: { operationalStateList: [0, 1, 2, 3].map(operationalStateId => ({ operationalStateId })), operationalState: 0,
    phaseList: ['Wash', 'Rinse', 'Spin'], currentPhase: null, operationalError: { errorStateId: 0 } },
}));
const ac = await aggregator.add(new Endpoint(RoomAirConditionerDevice.with(ACOnOff, ACThermostat, HemsServer, BridgedDeviceBasicInformationServer), {
  id: 'ac-1', number: 3, bridgedDeviceBasicInformation: { nodeLabel: 'Virtual AC', uniqueId: 'hems-ac-1', reachable: false },
  hemsTask: { snapshot: '', result: '', clusterRevision: 1 },
  thermostat: { controlSequenceOfOperation: 0, systemMode: 0, occupiedCoolingSetpoint: 2700, localTemperature: 3000,
    minCoolSetpointLimit: 1600, maxCoolSetpointLimit: 3000, absMinCoolSetpointLimit: 1600, absMaxCoolSetpointLimit: 3000 },
}));
const bulb = await aggregator.add(new Endpoint(ColorTemperatureLightDevice.with(BulbOnOff, BulbLevel, BulbColor, HemsServer, BridgedDeviceBasicInformationServer), {
  id: 'bulb-1', number: 4, bridgedDeviceBasicInformation: { nodeLabel: 'Virtual living room bulb', uniqueId: 'hems-bulb-1', reachable: false },
  hemsTask: { snapshot: '', result: '', clusterRevision: 1 },
  onOff: { onOff: false },
  levelControl: { currentLevel: 254 },
  colorControl: { colorMode: 2, enhancedColorMode: 2, colorTemperatureMireds: 370,
    colorTempPhysicalMinMireds: 154, colorTempPhysicalMaxMireds: 370,
    coupleColorTempToLevelMinMireds: 154 },
}));
endpoints.set('washer-1', washer); endpoints.set('ac-1', ac); endpoints.set('bulb-1', bulb);
const subscribe = () => mqttClient.subscribe(['washer-1', 'ac-1', 'bulb-1'].flatMap(device => ['state', 'result'].map(kind => `${prefix}/devices/${device}/${kind}`)), { qos: 1 });
mqttClient.on('connect', subscribe);
let updates = Promise.resolve();
mqttClient.on('message', (topic, bytes) => {
  if (bytes.length > 65536) return;
  try {
    const item = JSON.parse(bytes.toString('utf8')), kind = topic.split('/').at(-1), device = topic.split('/').at(-2);
    if (!endpoints.has(device) || item.device_id !== device) return;
    if (kind === 'result') { pending.get(item.command_id)?.(item); pending.delete(item.command_id); return; }
    snapshots.set(device, item);
    const endpoint = endpoints.get(device);
    updates = updates.then(async () => {
      await endpoint.set({ hemsTask: { snapshot: JSON.stringify(item) }, bridgedDeviceBasicInformation: { reachable: true } });
      if (device === 'washer-1') {
        await endpoint.set({ operationalState: { operationalState: item.status === 'fault' ? 3 : item.status === 'running' ? 1 : 0,
          currentPhase: item.status === 'running' ? Math.min(2, Math.floor(item.progress * 3)) : null } });
      } else if (device === 'bulb-1') {
        await endpoint.set({ onOff: { onOff: item.power }, levelControl: { currentLevel: item.level },
          colorControl: { colorTemperatureMireds: item.mired, colorMode: 2 } });
      } else {
        await endpoint.set({ onOff: { onOff: item.status === 'running' }, thermostat: {
          localTemperature: Math.round(item.temperature_c * 100), occupiedCoolingSetpoint: Math.round(item.target_c * 100),
          systemMode: item.status === 'running' ? 3 : 0 } });
      }
    }).catch(error => console.error('State update failed:', error.message));
  } catch (error) { console.error('Ignored invalid MQTT message:', error.message); }
});
mqttClient.on('error', error => console.error('MQTT:', error.message));
if (mqttClient.connected) subscribe();
await node.start();
emit({ ready: true, role: 'bridge', port });
let stopping = false;
async function stop() { if (stopping) return; stopping = true; mqttClient.end(true); await node.close(); process.exit(0); }
process.on('SIGINT', stop); process.on('SIGTERM', stop);
