import { ServerNode, ClientBehavior, Seconds, ControllerBehavior } from '@matter/main';
import { Read, Invoke, Write } from '@matter/protocol';
import { OperationalState } from '@matter/types/clusters/operational-state';
import { OnOff } from '@matter/types/clusters/on-off';
import { Thermostat } from '@matter/types/clusters/thermostat';
import { deflateRawSync } from 'node:zlib';
import { createInterface } from 'node:readline';
import { Hems, root, emit, port, passcode, discriminator, basicInformation } from './common.mjs';

const HemsClient = ClientBehavior(Hems);
const node = await ServerNode.create(ServerNode.RootEndpoint.with(ControllerBehavior), { id: 'hems-controller',
  network: { port: port + 1 }, controller: { adminFabricLabel: 'HEMS local', ble: false },
  commissioning: { enabled: false }, basicInformation: basicInformation('HEMS controller') });
await node.start();
let peer = [...node.peers].find(item => item.lifecycle.isCommissioned);
if (!peer) peer = await node.peers.commission({ discriminator, passcode, timeout: Seconds(40),
  onAttestationFailure: () => true, discoveryCapabilities: { onIpNetwork: true, ble: false } });
await peer.start();
const numbers = { 'washer-1': 2, 'ac-1': 3 };

async function read(device) {
  const number = numbers[device];
  if (!number) throw new Error('Unknown device');
  // Force an actual Matter read. Cached attributes cannot prove connectivity.
  for await (const _ of peer.interaction.read({ ...Read(Read.Attribute({ endpoint: number, cluster: Hems })), includeKnownVersions: true }));
  const values = peer.endpoints.for(number).stateOf(HemsClient);
  if (values.snapshot) emit({ kind: 'state', device_id: device, item: JSON.parse(values.snapshot) });
  if (values.result) emit({ kind: 'result', device_id: device, item: JSON.parse(values.result) });
  emit({ kind: 'health', device_id: device, connected: true });
}

async function invoke(endpoint, cluster, command, fields) {
  for await (const result of peer.interaction.invoke(Invoke({ endpoint, cluster, command, ...(fields === undefined ? {} : { fields }) }))) {
    if (result.kind?.includes('status') && result.status?.status) throw new Error('Matter operation rejected');
  }
}

async function send(command) {
  const endpoint = numbers[command.device_id];
  if (!endpoint || Date.parse(command.expires_at) <= Date.now()) throw new Error('Invalid or expired command');
  const packed = deflateRawSync(Buffer.from(JSON.stringify(command))).toString('base64');
  const count = Math.ceil(packed.length / 500);
  for (let index = 0; index < count; index++) await invoke(endpoint, Hems, 'prepare', {
    id: command.command_id, index, count, chunk: packed.slice(index * 500, (index + 1) * 500) });
  if (command.action === 'start' || command.action === 'stop') {
    if (command.device_id === 'washer-1') await invoke(endpoint, OperationalState, command.action);
    else {
      if (command.action === 'start') {
        const result = await peer.interaction.write(Write(Write.Attribute({ endpoint, cluster: Thermostat,
          attributes: 'occupiedCoolingSetpoint', value: Math.round(command.plan.constraints.target_c * 100) })));
        if (result.some?.(entry => entry.status?.status)) throw new Error('Thermostat write rejected');
      }
      await invoke(endpoint, OnOff, command.action === 'start' ? 'on' : 'off');
    }
  } else await invoke(endpoint, Hems, 'auxiliary');
  await read(command.device_id);
}

emit({ ready: true, role: 'controller' });
let chain = Promise.resolve(), stopping = false;
const enqueue = work => { chain = chain.then(work).catch(error => console.error('Matter:', error.message)); };
const lines = createInterface({ input: process.stdin });
lines.on('line', line => enqueue(async () => {
  let request;
  try {
    if (line.length > 65536) throw new Error('Request too large');
    request = JSON.parse(line);
    if (request.op === 'command') await send(request.command);
    else if (request.op === 'read') await read(request.device_id);
    else throw new Error('Unknown controller operation');
    emit({ id: request.id, ok: true });
  } catch (error) {
    emit({ id: request?.id, ok: false, device_id: request?.command?.device_id || request?.device_id, error: error.message });
  }
}));
let polling = false;
const timer = setInterval(() => {
  if (polling || stopping) return;
  polling = true;
  enqueue(async () => {
    try { for (const device of Object.keys(numbers)) await read(device); }
    catch (error) { for (const device of Object.keys(numbers)) emit({ kind: 'health', device_id: device, connected: false, error: error.message }); }
    finally { polling = false; }
  });
}, 1000);
async function stop() { if (stopping) return; stopping = true; clearInterval(timer); await node.close(); process.exit(0); }
lines.on('close', stop);
process.on('SIGINT', stop); process.on('SIGTERM', stop);
