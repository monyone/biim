#!/usr/bin/env python3

from typing import cast

import argparse
import os
import sys

from pathlib import Path

from biim.mpeg2ts import ts
from biim.mpeg2ts.pat import PATSection
from biim.mpeg2ts.pmt import PMTSection
from biim.mpeg2ts.pes import PES
from biim.mpeg2ts.parser import SectionParser
from biim.mpeg2ts.packetize import packetize_section


if __name__ == '__main__':
  parser = argparse.ArgumentParser(description=('seek'))

  parser.add_argument('-i', '--input', type=Path, required=True)
  parser.add_argument('-s', '--start', type=float, nargs='?', default=0)
  parser.add_argument('-n', '--SID', type=int, nargs='?')

  args = parser.parse_args()

  PAT_Parser: SectionParser[PATSection] = SectionParser(PATSection)
  PMT_Parser: SectionParser[PMTSection] = SectionParser(PMTSection)

  PAT_BYTES = bytes()
  PMT_BYTES = bytes()

  PMT_PID: int | None = None
  FIRST_VIDEO_PID: int | None = None
  FIRST_VIDEO_DTS: int | None = None

  with open(args.input, 'rb') as reader:
    while True:
      isEOF = False
      while True:
        sync_byte = reader.read(1)
        if sync_byte == ts.SYNC_BYTE:
          break
        elif sync_byte == b'':
          isEOF = True
          break

      packet = ts.SYNC_BYTE + reader.read(ts.PACKET_SIZE - 1)
      if len(packet) != 188:
        break

      PID = ts.pid(packet)
      if PID == 0x00:
        PAT_Parser.push(packet)
        for PAT in PAT_Parser:
          if PAT.CRC32() != 0: continue
          PAT_BYTES = b''.join(packetize_section(PAT, False, False, PID, 0, 0))

          for program_number, program_map_PID in PAT:
            if program_number == 0: continue

            if program_number == args.SID:
              PMT_PID = program_map_PID
            elif not PMT_PID and not args.SID:
              PMT_PID = program_map_PID

      elif PID == PMT_PID:
        PMT_Parser.push(packet)
        for PMT in PMT_Parser:
          if PMT.CRC32() != 0: continue
          if FIRST_VIDEO_PID is not None: continue
          PMT_BYTES = b''.join(packetize_section(PMT, False, False, PID, 0, 0))

          for stream_type, elementary_PID, _ in PMT:
            if stream_type == 0x02 or stream_type == 0x1b or stream_type == 0x24:
              FIRST_VIDEO_PID = elementary_PID
          break

  with open(args.input, 'rb') as reader:
    while True:
      isEOF = False
      while True:
        sync_byte = reader.read(1)
        if sync_byte == ts.SYNC_BYTE:
          break
        elif sync_byte == b'':
          isEOF = True
          break

      packet = ts.SYNC_BYTE + reader.read(ts.PACKET_SIZE - 1)
      if len(packet) != 188:
        break

      PID = ts.pid(packet)
      if PID == FIRST_VIDEO_PID:
        if ts.payload_unit_start_indicator(packet):
          VIDEO_PES = PES(ts.payload(packet))
          FIRST_VIDEO_DTS = VIDEO_PES.dts() if VIDEO_PES.has_dts() else VIDEO_PES.pts()
          break

  if FIRST_VIDEO_DTS is None:
    exit(-1)

  seek_point = 0
  with open(args.input, 'rb') as reader:
    reader.seek(0, os.SEEK_END)
    lower, upper = 0, reader.tell()
    while lower + 1 < upper:
      print(lower, upper, file=sys.stderr)
      reader.seek((lower + upper) // 2)

      while True:
        while True:
          sync_byte = reader.read(1)
          if sync_byte == ts.SYNC_BYTE:
            break
          elif sync_byte == b'':
            upper = (lower + upper) // 2
            break

        packet = ts.SYNC_BYTE + reader.read(ts.PACKET_SIZE - 1)
        if len(packet) != 188:
          upper = (lower + upper) // 2
          break

        PID = ts.pid(packet)

        if PID == FIRST_VIDEO_PID:
          if ts.payload_unit_start_indicator(packet):
            VIDEO_PES = PES(ts.payload(packet))
            timestamp = cast(int, VIDEO_PES.dts() if VIDEO_PES.has_dts() else VIDEO_PES.pts())
            DIFF = ((timestamp - cast(int, FIRST_VIDEO_DTS) + ts.PCR_CYCLE) % ts.PCR_CYCLE) / ts.HZ
            print("TS:", timestamp, FIRST_VIDEO_DTS, DIFF, file=sys.stderr)

            if DIFF <= args.start:
              lower = (lower + upper) // 2
            else:
              upper = (lower + upper) // 2
            break
    seek_point = lower

  with open(args.input, 'rb') as reader:
    reader.seek(seek_point)
    sys.stdout.buffer.write(PAT_BYTES)
    sys.stdout.buffer.write(PMT_BYTES)
    while True:
      while True:
        sync_byte = reader.read(1)
        if sync_byte == ts.SYNC_BYTE:
          break
        elif sync_byte == b'':
          exit(0)

      packet = ts.SYNC_BYTE + reader.read(ts.PACKET_SIZE - 1)
      if len(packet) != 188:
        exit(0)
      sys.stdout.buffer.write(packet)
