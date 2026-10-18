// Project extension carries approval context; operations use standard Matter clusters.
import { ClusterType, Attribute, Command } from '@matter/types/cluster';
import { TlvObject, TlvField } from '@matter/types/tlv';
import { TlvString, TlvUInt16 } from '@matter/types/tlv';
import { TlvVoid as TlvNoResponse } from '@matter/types/tlv';
import { ClusterBehavior, Matter, Environment } from '@matter/main';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export const Hems = ClusterType({
  id: 0xfff1fc00, name: 'HemsTask', revision: 1,
  attributes: { snapshot: Attribute(0, TlvString.bound({ maxLength: 65536 }), { default: '' }),
                result: Attribute(1, TlvString.bound({ maxLength: 2048 }), { default: '' }) },
  commands: {
    prepare: Command(0, TlvObject({ id: TlvField(0, TlvString.bound({ maxLength: 64 })),
      index: TlvField(1, TlvUInt16), count: TlvField(2, TlvUInt16), chunk: TlvField(3, TlvString.bound({ maxLength: 600 })) }), 0, TlvNoResponse),
    auxiliary: Command(1, TlvObject({}), 1, TlvNoResponse),
  },
});
// The pinned SDK's legacy TLV adapter leaves mandatory conformance empty.
// Make it explicit so these fields and commands are actually advertised.
for (const element of Hems.schema.children) {
  element.conformance = 'M';
  for (const field of element.children) field.conformance = 'M';
}
Matter.children.push(Hems.schema);
export const HemsBase = ClusterBehavior.for(Hems);
export const root = resolve(fileURLToPath(new URL('../', import.meta.url)), process.env.HEMS_MATTER_STORAGE || 'artifacts/matter');
Environment.default.vars.set('storage.path', root);
Environment.default.vars.set('log.level', process.env.MATTER_LOG_LEVEL || 'error');
export const wireWrite = process.stdout.write.bind(process.stdout);
// Matter diagnostics belong on stderr; stdout is the Python JSON-lines channel.
process.stdout.write = (...args) => process.stderr.write(...args);
export const emit = value => wireWrite(JSON.stringify(value) + '\n');
export const port = Number(process.env.HEMS_MATTER_PORT || 5540);
export const passcode = Number(process.env.HEMS_MATTER_PASSCODE || 20202021);
export const discriminator = Number(process.env.HEMS_MATTER_DISCRIMINATOR || 3840);
export const basicInformation = name => ({ vendorName: 'HEMS local demo', vendorId: 0xfff1,
  productName: name, productId: 0x8000, nodeLabel: name, serialNumber: name,
  hardwareVersion: 1, hardwareVersionString: '1', softwareVersion: 1, softwareVersionString: '0.1.0' });
