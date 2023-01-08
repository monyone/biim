#!/usr/bin/env python3

import asyncio
from aiohttp import web

import argparse
import sys
import os
import time

from collections import deque

from pathlib import Path
from datetime import datetime, timedelta

from mpeg2ts import ts
from mpeg2ts.packetize import packetize_section, packetize_pes
from mpeg2ts.section import Section
from mpeg2ts.pat import PATSection
from mpeg2ts.pmt import PMTSection
from mpeg2ts.pes import PES
from mpeg2ts.h264 import H264PES
from mpeg2ts.h265 import H265PES
from mpeg2ts.parser import SectionParser, PESParser

from hls.m3u8 import M3U8

from mp4.box import ftyp, moov, mvhd, mvex, trex, moof, mdat, emsg
from mp4.avc import avcTrack
from mp4.hevc import hevcTrack
from mp4.mp4a import mp4aTrack

SAMPLING_FREQUENCY = {
  0x00: 96000,
  0x01: 88200,
  0x02: 64000,
  0x03: 48000,
  0x04: 44100,
  0x05: 32000,
  0x06: 24000,
  0x07: 22050,
  0x08: 16000,
  0x09: 12000,
  0x0a: 11025,
  0x0b: 8000,
  0x0c: 7350,
}

class BlockingPipeReader:
  def __init__(self, reader, size=ts.PACKET_SIZE * 16):
    self.reader = reader
    self.buffer = bytearray()
    self.size = size

  async def __fill(self):
    data = await asyncio.to_thread(lambda: self.reader.read(self.size))
    if data == b'': return False
    self.buffer += data
    return True

  async def read(self, n):
    while len(self.buffer) < n:
      if not (await self.__fill()): break
    result = self.buffer[:n]
    self.buffer = self.buffer[n:]
    return result

  async def readexactly(self, n):
    while len(self.buffer) < n:
      if not (await self.__fill()): raise asyncio.IncompleteReadError(self.buffer, None)
    result = self.buffer[:n]
    self.buffer = self.buffer[n:]
    return result

async def main():
  loop = asyncio.get_running_loop()
  parser = argparse.ArgumentParser(description=('biim: LL-HLS origin'))

  parser.add_argument('-i', '--input', type=argparse.FileType('rb'), nargs='?', default=sys.stdin.buffer)
  parser.add_argument('-s', '--SID', type=int, nargs='?')
  parser.add_argument('-l', '--list_size', type=int, nargs='?')
  parser.add_argument('-t', '--target_duration', type=int, nargs='?', default=1)
  parser.add_argument('-p', '--part_duration', type=float, nargs='?', default=0.1)
  parser.add_argument('--port', type=int, nargs='?', default=8080)

  args = parser.parse_args()

  m3u8 = M3U8(args.target_duration, args.part_duration, args.list_size, True)
  init = asyncio.Future()

  async def playlist(request):
    nonlocal m3u8
    msn = request.query['_HLS_msn'] if '_HLS_msn' in request.query else None
    part = request.query['_HLS_part'] if '_HLS_part' in request.query else None

    if msn is None and part is None:
      future = m3u8.plain()
      if future is None:
        return web.Response(headers={'Access-Control-Allow-Origin': '*'}, status=400, content_type="application/x-mpegURL")

      result = await future
      return web.Response(headers={'Access-Control-Allow-Origin': '*'}, text=result, content_type="application/x-mpegURL")
    else:
      msn = int(msn)
      if part is None: part = 0
      part = int(part)
      future = m3u8.blocking(msn, part)
      if future is None:
        return web.Response(headers={'Access-Control-Allow-Origin': '*'}, status=400, content_type="application/x-mpegURL")

      result = await future
      return web.Response(headers={'Access-Control-Allow-Origin': '*'}, text=result, content_type="application/x-mpegURL")
  async def segment(request):
    nonlocal m3u8
    msn = request.query['msn'] if 'msn' in request.query else None

    if msn is None: msn = 0
    msn = int(msn)
    queue = await m3u8.segment(msn)
    if queue is None:
      return web.Response(headers={'Access-Control-Allow-Origin': '*'}, status=400, content_type="video/mp4")

    response = web.StreamResponse(headers={'Access-Control-Allow-Origin': '*', 'Content-Type': 'video/mp4'}, status=200)
    await response.prepare(request)

    while True:
      stream = await queue.get()
      if stream == None : break
      await response.write(stream)

    await response.write_eof()
    return response
  async def partial(request):
    nonlocal m3u8
    msn = request.query['msn'] if 'msn' in request.query else None
    part = request.query['part'] if 'part' in request.query else None

    if msn is None: msn = 0
    msn = int(msn)
    if part is None: part = 0
    part = int(part)
    queue = await m3u8.partial(msn, part)
    if queue is None:
      return web.Response(headers={'Access-Control-Allow-Origin': '*'}, status=400, content_type="video/mp4")

    response = web.StreamResponse(headers={'Access-Control-Allow-Origin': '*', 'Content-Type': 'video/mp4'}, status=200)
    await response.prepare(request)

    while True:
      stream = await queue.get()
      if stream == None : break
      await response.write(stream)

    await response.write_eof()
    return response
  async def initalization(request):
    if init is None:
      return web.Response(headers={'Access-Control-Allow-Origin': '*'}, status=400, content_type="video/mp4")

    body = await asyncio.shield(init)
    return web.Response(headers={'Access-Control-Allow-Origin': '*'}, body=body, content_type="video/mp4")

  # setup aiohttp
  app = web.Application()
  app.add_routes([
    web.get('/playlist.m3u8', playlist),
    web.get('/segment', segment),
    web.get('/part', partial),
    web.get('/init', initalization),
  ])
  runner = web.AppRunner(app)
  await runner.setup()
  await loop.create_server(runner.server, '0.0.0.0', args.port)

  # setup reader
  PAT_Parser = SectionParser(PATSection)
  PMT_Parser = SectionParser(PMTSection)
  AAC_PES_Parser = PESParser(PES)
  H264_PES_Parser = PESParser(H264PES)
  H265_PES_Parser = PESParser(H265PES)
  ID3_PES_Parser = PESParser(PES)

  PCR_PID = None
  LATEST_PCR_VALUE = None
  LATEST_PCR_MONOTONIC_TIME = None
  LATEST_PCR_SLEEP_DIFFERENCE = 0
  LATEST_PCR_TIMESTAMP_90KHZ = 0

  PMT_PID = None
  AAC_PID = None
  H264_PID = None
  H265_PID = None
  ID3_PID = None

  AAC_CONFIG = None

  CURR_H264 = None
  NEXT_H264 = None
  CURR_H265 = None
  NEXT_H265 = None

  H264_FRAGMENTS = deque()
  H265_FRAGMENTS = deque()
  AAC_FRAGMENTS = deque()
  EMSG_FRAGMENTS = deque()

  VPS = None
  SPS = None
  PPS = None

  INITIALIZATION_SEGMENT_DISPATCHED = False
  PARTIAL_BEGIN_TIMESTAMP = None

  if args.input is not sys.stdin.buffer or os.name == 'nt':
    reader = BlockingPipeReader(args.input)
  else:
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, args.input)

  while True:
    isEOF = False
    while True:
      sync_byte = await reader.read(1)
      if sync_byte == ts.SYNC_BYTE:
        break
      elif sync_byte == b'':
        isEOF = True
        break
    if isEOF:
      break

    packet = None
    try:
      packet = ts.SYNC_BYTE + await reader.readexactly(ts.PACKET_SIZE - 1)
    except asyncio.IncompleteReadError:
      break

    PID = ts.pid(packet)
    if PID == H264_PID:
      H264_PES_Parser.push(packet)
      for H264 in H264_PES_Parser:
        if LATEST_PCR_VALUE is None: continue
        timestamp = (((H264.dts() if H264.has_dts() else H264.pts()) - LATEST_PCR_VALUE + ts.PCR_CYCLE) % ts.PCR_CYCLE) + LATEST_PCR_TIMESTAMP_90KHZ
        cts = (H264.pts() - (H264.dts() if H264.has_dts() else H264.pts()) + ts.PCR_CYCLE) % ts.PCR_CYCLE
        keyInSamples = False
        samples = deque()

        for ebsp in H264:
          nal_unit_type = ebsp[0] & 0x1f

          if nal_unit_type == 0x07: # SPS
            SPS = ebsp
          elif nal_unit_type == 0x08: # PPS
            PPS = ebsp
          elif nal_unit_type == 0x09 or nal_unit_type == 0x06: # AUD or SEI
            pass
          elif nal_unit_type == 0x05:
            keyInSamples = True
            samples.append(ebsp)
          else:
            samples.append(ebsp)
        NEXT_H264 = (keyInSamples, samples, timestamp, cts) if samples else None

        hasIDR = False
        if CURR_H264:
          isKeyframe, samples, dts, cts = CURR_H264
          hasIDR = isKeyframe
          duration = timestamp - dts
          content = bytearray()
          while samples:
            ebsp = samples.popleft()
            content += len(ebsp).to_bytes(4, byteorder='big') + ebsp

          H264_FRAGMENTS.append(
            b''.join([
              moof(0,
                [
                  (1, duration, dts, 0, [(len(content), duration, isKeyframe, cts)])
                ]
              ),
              mdat(content)
            ])
          )
        NEXT_H264, CURR_H264 = CURR_H264, NEXT_H264

        if SPS and PPS and AAC_CONFIG and not INITIALIZATION_SEGMENT_DISPATCHED:
          init.set_result(b''.join([
            ftyp(),
            moov(
              mvhd(ts.HZ),
              mvex([
                trex(1),
                trex(2)
              ]),
              [
                avcTrack(1, ts.HZ, SPS, PPS),
                mp4aTrack(2, ts.HZ, *AAC_CONFIG),
              ]
            )
          ]))
          INITIALIZATION_SEGMENT_DISPATCHED = True

        if hasIDR:
          if PARTIAL_BEGIN_TIMESTAMP is not None:
            PART_DIFF = timestamp - PARTIAL_BEGIN_TIMESTAMP
            if args.part_duration * ts.HZ < PART_DIFF:
              PARTIAL_BEGIN_TIMESTAMP = int(timestamp - max(0, PART_DIFF - args.part_duration * ts.HZ))
              m3u8.continuousPartial(PARTIAL_BEGIN_TIMESTAMP, False)
          PARTIAL_BEGIN_TIMESTAMP = timestamp
          m3u8.continuousSegment(PARTIAL_BEGIN_TIMESTAMP, True)
        elif PARTIAL_BEGIN_TIMESTAMP is not None:
          PART_DIFF = timestamp - PARTIAL_BEGIN_TIMESTAMP
          if args.part_duration * ts.HZ <= PART_DIFF:
            PARTIAL_BEGIN_TIMESTAMP = int(timestamp - max(0, PART_DIFF - args.part_duration * ts.HZ))
            m3u8.continuousPartial(PARTIAL_BEGIN_TIMESTAMP)

        while (EMSG_FRAGMENTS): m3u8.push(EMSG_FRAGMENTS.popleft())
        while (H264_FRAGMENTS): m3u8.push(H264_FRAGMENTS.popleft())
        while (AAC_FRAGMENTS): m3u8.push(AAC_FRAGMENTS.popleft())

    elif PID == H265_PID:
      H265_PES_Parser.push(packet)
      for H265 in H265_PES_Parser:
        if LATEST_PCR_VALUE is None: continue
        timestamp = (((H265.dts() if H265.has_dts() else  H265.pts()) - LATEST_PCR_VALUE + ts.PCR_CYCLE) % ts.PCR_CYCLE) + LATEST_PCR_TIMESTAMP_90KHZ
        cts = (H265.pts() - (H265.dts() if H265.has_dts() else  H265.pts()) + ts.PCR_CYCLE) % ts.PCR_CYCLE
        keyInSamples = False
        samples = deque()

        for ebsp in H265:
          nal_unit_type = (ebsp[0] >> 1) & 0x3f

          if nal_unit_type == 0x20: # VPS
            VPS = ebsp
          elif nal_unit_type == 0x21: # SPS
            SPS = ebsp
          elif nal_unit_type == 0x22: # PPS
            PPS = ebsp
          elif nal_unit_type == 0x23 or nal_unit_type == 0x27: # AUD or SEI
            pass
          elif nal_unit_type == 19 or nal_unit_type == 20 or nal_unit_type == 21: # IDR_W_RADL, IDR_W_LP, CRA_NUT
            keyInSamples = True
            samples.append(ebsp)
          else:
            samples.append(ebsp)
        NEXT_H265 = (keyInSamples, samples, timestamp, cts) if samples else None

        hasIDR = False
        if CURR_H265:
          isKeyframe, samples, dts, cts = CURR_H265
          hasIDR = isKeyframe
          duration = timestamp - dts
          content = bytearray()
          while samples:
            ebsp = samples.popleft()
            content += len(ebsp).to_bytes(4, byteorder='big') + ebsp

          H265_FRAGMENTS.append(
            b''.join([
              moof(0,
                [
                  (1, duration, dts, 0, [(len(content), duration, isKeyframe, cts)])
                ]
              ),
              mdat(content)
            ])
          )
        NEXT_H265, CURR_H265 = CURR_H265, NEXT_H265

        if VPS and SPS and PPS and AAC_CONFIG and not INITIALIZATION_SEGMENT_DISPATCHED:
          init.set_result(b''.join([
            ftyp(),
            moov(
              mvhd(ts.HZ),
              mvex([
                trex(1),
                trex(2)
              ]),
              [
                hevcTrack(1, ts.HZ, VPS, SPS, PPS),
                mp4aTrack(2, ts.HZ, *AAC_CONFIG),
              ]
            )
          ]))
          INITIALIZATION_SEGMENT_DISPATCHED = True

        if hasIDR:
          if PARTIAL_BEGIN_TIMESTAMP is not None:
            PART_DIFF = timestamp - PARTIAL_BEGIN_TIMESTAMP
            if args.part_duration * ts.HZ < PART_DIFF:
              PARTIAL_BEGIN_TIMESTAMP = int(timestamp - max(0, PART_DIFF - args.part_duration * ts.HZ))
              m3u8.continuousPartial(PARTIAL_BEGIN_TIMESTAMP, False)
          PARTIAL_BEGIN_TIMESTAMP = timestamp
          m3u8.continuousSegment(PARTIAL_BEGIN_TIMESTAMP, True)
        elif PARTIAL_BEGIN_TIMESTAMP is not None:
          PART_DIFF = timestamp - PARTIAL_BEGIN_TIMESTAMP
          if args.part_duration * ts.HZ <= PART_DIFF:
            PARTIAL_BEGIN_TIMESTAMP = int(timestamp - max(0, PART_DIFF - args.part_duration * ts.HZ))
            m3u8.continuousPartial(PARTIAL_BEGIN_TIMESTAMP)

        while (EMSG_FRAGMENTS): m3u8.push(EMSG_FRAGMENTS.popleft())
        while (H265_FRAGMENTS): m3u8.push(H265_FRAGMENTS.popleft())
        while (AAC_FRAGMENTS): m3u8.push(AAC_FRAGMENTS.popleft())

    elif PID == AAC_PID:
      AAC_PES_Parser.push(packet)
      for AAC_PES in AAC_PES_Parser:
        if LATEST_PCR_VALUE is None: continue
        timestamp = ((AAC_PES.pts() - LATEST_PCR_VALUE + ts.PCR_CYCLE) % ts.PCR_CYCLE) + LATEST_PCR_TIMESTAMP_90KHZ
        begin, ADTS_AAC = 0, AAC_PES.PES_packet_data()
        length = len(ADTS_AAC)
        while begin < length:
          protection = (ADTS_AAC[begin + 1] & 0b00000001) == 0
          profile = ((ADTS_AAC[begin + 2] & 0b11000000) >> 6)
          samplingFrequencyIndex = ((ADTS_AAC[begin + 2] & 0b00111100) >> 2)
          channelConfiguration = ((ADTS_AAC[begin + 2] & 0b00000001) << 2) | ((ADTS_AAC[begin + 3] & 0b11000000) >> 6)
          frameLength = ((ADTS_AAC[begin + 3] & 0x03) << 11) | (ADTS_AAC[begin + 4] << 3) | ((ADTS_AAC[begin + 5] & 0xE0) >> 5)
          if not AAC_CONFIG:
            AAC_CONFIG = (bytes([
              ((profile + 1) << 3) | ((samplingFrequencyIndex & 0x0E) >> 1),
              ((samplingFrequencyIndex & 0x01) << 7) | (channelConfiguration << 3)
            ]), channelConfiguration, SAMPLING_FREQUENCY[samplingFrequencyIndex])
          duration = 1024 * ts.HZ // SAMPLING_FREQUENCY[samplingFrequencyIndex]
          AAC_FRAGMENTS.append(
            b''.join([
              moof(0,
                [
                  (2, duration, timestamp, 0, [(frameLength - (9 if protection else 7), duration, False, 0)])
                ]
              ),
              mdat(bytes(ADTS_AAC[begin + (9 if protection else 7): begin + frameLength]))
            ])
          )
          timestamp += duration
          begin += frameLength

    elif PID == 0x00:
      PAT_Parser.push(packet)
      for PAT in PAT_Parser:
        if PAT.CRC32() != 0: continue
        LAST_PAT = PAT

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
        LAST_PMT = PMT

        PCR_PID = PMT.PCR_PID
        for stream_type, elementary_PID in PMT:
          if stream_type == 0x1b:
            H264_PID = elementary_PID
          elif stream_type == 0x24:
            H265_PID = elementary_PID
          elif stream_type == 0x0F:
            AAC_PID = elementary_PID
          elif stream_type == 0x15:
            ID3_PID = elementary_PID

    elif PID == ID3_PID:
      ID3_PES_Parser.push(packet)
      for ID3_PES in ID3_PES_Parser:
        if LATEST_PCR_VALUE is None: continue
        timestamp = ((ID3_PES.pts() - LATEST_PCR_VALUE + ts.PCR_CYCLE) % ts.PCR_CYCLE) + LATEST_PCR_TIMESTAMP_90KHZ
        ID3 = ID3_PES.PES_packet_data()
        EMSG_FRAGMENTS.append(emsg(ts.HZ, timestamp, None, 'https://aomedia.org/emsg/ID3', ID3))

    else:
      pass

    if PID == PCR_PID and ts.has_pcr(packet):
      PCR_VALUE = (ts.pcr(packet) - ts.HZ + ts.PCR_CYCLE) % ts.PCR_CYCLE
      if LATEST_PCR_VALUE is not None:
        PCR_DIFF = (PCR_VALUE - LATEST_PCR_VALUE + ts.PCR_CYCLE) % ts.PCR_CYCLE
        TIME_DIFF = (time.monotonic() - LATEST_PCR_MONOTONIC_TIME)
        LATEST_PCR_TIMESTAMP_90KHZ += PCR_DIFF
        if args.input is not sys.stdin.buffer:
          SLEEP_BEGIN = time.monotonic()
          await asyncio.sleep(max(0, PCR_DIFF / ts.HZ - (TIME_DIFF + LATEST_PCR_SLEEP_DIFFERENCE)))
          SLEEP_END = time.monotonic()
          LATEST_PCR_SLEEP_DIFFERENCE = (SLEEP_END - SLEEP_BEGIN) - max(0, PCR_DIFF / ts.HZ - (TIME_DIFF + LATEST_PCR_SLEEP_DIFFERENCE))
      LATEST_PCR_VALUE = PCR_VALUE
      LATEST_PCR_MONOTONIC_TIME = time.monotonic()

if __name__ == '__main__':
  asyncio.run(main())
