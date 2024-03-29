from typing import Type

from biim.mpeg2ts import ts
from biim.mpeg2ts.section import Section
from biim.mpeg2ts.pes import PES

def packetize_section(section: Section, transport_error_indicator: bool, transport_priority: bool, pid: int, transport_scrambling_control: int, continuity_counter: int) -> list[bytes]:
  result: list[bytes] = []
  begin = 0
  while (begin < len(section)):
    next = min(len(section), begin + (ts.PACKET_SIZE - ts.HEADER_SIZE) - (1 if begin == 0 else 0))
    result.append(bytes(
      ([
        ts.SYNC_BYTE[0],
        ((1 if transport_error_indicator else 0) << 7) | ((1 if begin == 0 else 0) << 6) | ((1 if transport_priority else 0) << 5) | ((pid & 0x1F00) >> 8),
        (pid & 0x00FF),
        (transport_scrambling_control << 6) | (1 << 4) | (continuity_counter & 0x0F),
      ]) +
      ([0] if begin == 0 else []) +
      list(section[begin:next]) +
      ([ts.STUFFING_BYTE[0]] * ((ts.PACKET_SIZE - ts.HEADER_SIZE) - ((next - begin) + (1 if begin == 0 else 0))))
    ))
    continuity_counter = (continuity_counter + 1) & 0x0F
    begin = next
  return result

def packetize_pes(pes: PES, transport_error_indicator: bool, transport_priority: bool, pid: int, transport_scrambling_control: int, continuity_counter: int) -> list[bytes]:
  result: list[bytes] = []
  begin = 0
  while (begin < len(pes)):
    next = min(len(pes), begin + (ts.PACKET_SIZE - ts.HEADER_SIZE))
    packet = bytearray()
    packet += bytes([
      ts.SYNC_BYTE[0],
      ((1 if transport_error_indicator else 0) << 7) | ((1 if begin == 0 else 0) << 6) | ((1 if transport_priority else 0) << 5) | ((pid & 0x1F00) >> 8),
      (pid & 0x00FF),
      (transport_scrambling_control << 6) | (0x30 if (ts.PACKET_SIZE - ts.HEADER_SIZE) > (next - begin) else 0x10) | (continuity_counter & 0x0F),
    ])
    if (((ts.PACKET_SIZE - ts.HEADER_SIZE) > (next - begin))):
      packet += bytes([((ts.PACKET_SIZE - ts.HEADER_SIZE) - (next - begin)) - 1])
    if (((ts.PACKET_SIZE - ts.HEADER_SIZE) > (next - begin + 1))):
      packet += b'\x00'
    if (((ts.PACKET_SIZE - ts.HEADER_SIZE) > (next - begin + 2))):
      packet += bytes([0xFF] * (((ts.PACKET_SIZE - ts.HEADER_SIZE) - (next - begin)) - 2))
    packet += bytes(pes[begin:next])
    result.append(bytes(packet))
    continuity_counter = (continuity_counter + 1) & 0x0F
    begin = next
  return result
