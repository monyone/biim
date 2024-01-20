#!/usr/bin/env python3

from typing import cast

import asyncio
from aiohttp import web

import argparse
import sys
import os
import time

from collections import deque

from datetime import datetime, timedelta, timezone

from biim.mpeg2ts import ts
from biim.mpeg2ts.pat import PATSection
from biim.mpeg2ts.pmt import PMTSection
from biim.mpeg2ts.scte import SpliceInfoSection, SpliceInsert, TimeSignal, SegmentationDescriptor
from biim.mpeg2ts.pes import PES
from biim.mpeg2ts.h264 import H264PES
from biim.mpeg2ts.h265 import H265PES
from biim.mpeg2ts.parser import SectionParser, PESParser

from biim.hls.m3u8 import M3U8

from biim.mp4.box import ftyp, moov, mvhd, mvex, trex, moof, mdat, emsg
from biim.mp4.avc import avcTrack
from biim.mp4.hevc import hevcTrack
from biim.mp4.mp4a import mp4aTrack

from biim.util.reader import BufferingAsyncReader

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

async def main():
  loop = asyncio.get_running_loop()
  parser = argparse.ArgumentParser(description=('biim: LL-HLS origin'))

  parser.add_argument('-i', '--input', type=argparse.FileType('rb'), nargs='?', default=sys.stdin.buffer)
  parser.add_argument('-s', '--SID', type=int, nargs='?')
  parser.add_argument('-w', '--window_size', type=int, nargs='?')
  parser.add_argument('-t', '--target_duration', type=int, nargs='?', default=1)
  parser.add_argument('-p', '--part_duration', type=float, nargs='?', default=0.1)
  parser.add_argument('--port', type=int, nargs='?', default=8080)

  args = parser.parse_args()

  m3u8 = M3U8(
    target_duration=args.target_duration,
    part_target=args.part_duration,
    window_size=args.window_size,
    hasInit=True
  )
  init: asyncio.Future[bytes] = asyncio.Future()

  async def playlist(request: web.Request) -> web.Response:
    nonlocal m3u8
    msn = request.query['_HLS_msn'] if '_HLS_msn' in request.query else None
    part = request.query['_HLS_part'] if '_HLS_part' in request.query else None
    skip = request.query['_HLS_skip'] == 'YES' if '_HLS_skip' in request.query else False

    if msn is None and part is None:
      future = m3u8.plain()
      if future is None:
        return web.Response(headers={'Access-Control-Allow-Origin': '*', 'Cache-Control': 'max-age=0'}, status=400, content_type="application/x-mpegURL")

      result = await future
      return web.Response(headers={'Access-Control-Allow-Origin': '*', 'Cache-Control': 'max-age=0'}, text=result, content_type="application/x-mpegURL")
    else:
      if msn is None:
        return web.Response(headers={'Access-Control-Allow-Origin': '*', 'Cache-Control': 'max-age=0'}, status=400, content_type="application/x-mpegURL")
      msn = int(msn)
      if part is None: part = 0
      part = int(part)
      future = m3u8.blocking(msn, part, skip)
      if future is None:
        return web.Response(headers={'Access-Control-Allow-Origin': '*', 'Cache-Control': 'max-age=0'}, status=400, content_type="application/x-mpegURL")

      result = await future
      return web.Response(headers={'Access-Control-Allow-Origin': '*', 'Cache-Control': 'max-age=36000'}, text=result, content_type="application/x-mpegURL")
  async def segment(request: web.Request) -> web.Response | web.StreamResponse:
    nonlocal m3u8
    msn = request.query['msn'] if 'msn' in request.query else None

    if msn is None:
      return web.Response(headers={'Access-Control-Allow-Origin': '*', 'Cache-Control': 'max-age=0'}, status=400, content_type="video/mp4")
    msn = int(msn)
    queue = await m3u8.segment(msn)
    if queue is None:
      return web.Response(headers={'Access-Control-Allow-Origin': '*', 'Cache-Control': 'max-age=0'}, status=400, content_type="video/mp4")

    response = web.StreamResponse(headers={'Access-Control-Allow-Origin': '*', 'Cache-Control': 'max-age=36000', 'Content-Type': 'video/mp4'}, status=200)
    await response.prepare(request)

    while True:
      stream = await queue.get()
      if stream == None : break
      await response.write(stream)

    await response.write_eof()
    return response
  async def partial(request: web.Request) -> web.Response | web.StreamResponse:
    nonlocal m3u8
    msn = request.query['msn'] if 'msn' in request.query else None
    part = request.query['part'] if 'part' in request.query else None

    if msn is None:
      return web.Response(headers={'Access-Control-Allow-Origin': '*', 'Cache-Control': 'max-age=0'}, status=400, content_type="video/mp4")
    msn = int(msn)
    if part is None:
      return web.Response(headers={'Access-Control-Allow-Origin': '*', 'Cache-Control': 'max-age=0'}, status=400, content_type="video/mp4")
    part = int(part)
    queue = await m3u8.partial(msn, part)
    if queue is None:
      return web.Response(headers={'Access-Control-Allow-Origin': '*', 'Cache-Control': 'max-age=0'}, status=400, content_type="video/mp4")

    response = web.StreamResponse(headers={'Access-Control-Allow-Origin': '*', 'Cache-Control': 'max-age=36000', 'Content-Type': 'video/mp4'}, status=200)
    await response.prepare(request)

    while True:
      stream = await queue.get()
      if stream == None : break
      await response.write(stream)

    await response.write_eof()
    return response
  async def initalization(request: web.Request) -> web.Response:
    if init is None:
      return web.Response(headers={'Access-Control-Allow-Origin': '*', 'Cache-Control': 'max-age=0'}, status=400, content_type="video/mp4")

    body = await asyncio.shield(init)
    return web.Response(headers={'Access-Control-Allow-Origin': '*', 'Cache-Control': 'max-age=36000'}, body=body, content_type="video/mp4")

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
  await loop.create_server(cast(web.Server, runner.server), '0.0.0.0', args.port)

  # setup reader
  PAT_Parser: SectionParser[PATSection] = SectionParser(PATSection)
  PMT_Parser: SectionParser[PMTSection] = SectionParser(PMTSection)
  AAC_PES_Parser: PESParser[PES] = PESParser(PES)
  H264_PES_Parser: PESParser[H264PES] = PESParser(H264PES)
  H265_PES_Parser: PESParser[H265PES] = PESParser(H265PES)
  ID3_PES_Parser: PESParser[PES] = PESParser(PES)
  SCTE35_Parser: SectionParser[SpliceInfoSection] = SectionParser(SpliceInfoSection)

  PCR_PID: int | None = None
  LATEST_PCR_VALUE: int | None = None
  LATEST_PCR_DATETIME: datetime | None = None
  LATEST_PCR_TIMESTAMP_90KHZ: int = 0

  LATEST_VIDEO_TIMESTAMP_90KHZ: int | None = None
  LATEST_VIDEO_MONOTONIC_TIME: float | None = None
  LATEST_VIDEO_SLEEP_DIFFERENCE: float = 0

  PMT_PID: int | None = None
  AAC_PID: int | None = None
  H264_PID: int | None = None
  H265_PID: int | None = None
  ID3_PID: int | None = None
  SCTE35_PID: int | None = None

  AAC_CONFIG: tuple[bytes, int, int] | None = None

  CURR_H264: tuple[bool, deque[bytes], int, int, datetime] | None= None
  NEXT_H264: tuple[bool, deque[bytes], int, int, datetime] | None = None
  CURR_H265: tuple[bool, deque[bytes], int, int, datetime] | None = None
  NEXT_H265: tuple[bool, deque[bytes], int, int, datetime] | None = None

  SCTE35_OUT_QUEUE: deque[tuple[str, datetime, datetime | None, dict]] = deque()
  SCTE35_IN_QUEUE: deque[tuple[str, datetime]] = deque()

  H264_FRAGMENTS: deque[bytes] = deque()
  H265_FRAGMENTS: deque[bytes] = deque()
  AAC_FRAGMENTS: deque[bytes] = deque()
  EMSG_FRAGMENTS: deque[bytes] = deque()

  VPS: bytes | None = None
  SPS: bytes | None = None
  PPS: bytes | None = None

  INITIALIZATION_SEGMENT_DISPATCHED = False
  PARTIAL_BEGIN_TIMESTAMP: int | None = None

  if args.input is not sys.stdin.buffer or os.name == 'nt':
    reader = BufferingAsyncReader(args.input, ts.PACKET_SIZE * 16)
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
        if LATEST_PCR_DATETIME is None: continue
        dts = cast(int, H264.dts() if H264.has_dts() else H264.pts())
        pts = cast(int, H264.pts())
        cts = (pts - dts + ts.PCR_CYCLE) % ts.PCR_CYCLE
        timestamp = ((dts - LATEST_PCR_VALUE + ts.PCR_CYCLE) % ts.PCR_CYCLE) + LATEST_PCR_TIMESTAMP_90KHZ
        program_date_time: datetime = LATEST_PCR_DATETIME + timedelta(seconds=(((dts - LATEST_PCR_VALUE + ts.PCR_CYCLE) % ts.PCR_CYCLE) / ts.HZ))
        keyframe_in_samples = False
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
            keyframe_in_samples = True
            samples.append(ebsp)
          else:
            samples.append(ebsp)
        NEXT_H264 = (keyframe_in_samples, samples, timestamp, cts, program_date_time) if samples else None

        has_IDR = False
        begin_timestamp: int | None = None
        begin_program_date_time: datetime | None = None
        if CURR_H264:
          has_key_frame, samples, dts, cts, pdt = CURR_H264
          has_IDR = has_key_frame
          begin_timestamp = dts
          begin_program_date_time = pdt
          duration = timestamp - dts
          content = bytearray()
          while samples:
            ebsp = samples.popleft()
            content += len(ebsp).to_bytes(4, byteorder='big') + ebsp

          H264_FRAGMENTS.append(
            b''.join([
              moof(0,
                [
                  (1, duration, dts, 0, [(len(content), duration, has_key_frame, cts)])
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
              b''.join([
                avcTrack(1, ts.HZ, SPS, PPS),
                mp4aTrack(2, ts.HZ, *AAC_CONFIG),
              ])
            )
          ]))
          INITIALIZATION_SEGMENT_DISPATCHED = True

        if begin_timestamp is None: continue

        if has_IDR and begin_program_date_time is not None:
          while SCTE35_OUT_QUEUE:
            if SCTE35_OUT_QUEUE[0][1] <= begin_program_date_time:
              id, start_date, end_date, attributes = SCTE35_OUT_QUEUE.popleft()
              m3u8.open(id, begin_program_date_time, end_date, **attributes)
            else: break
          while SCTE35_IN_QUEUE:
            if SCTE35_IN_QUEUE[0][1] <= begin_program_date_time:
              id, end_date = SCTE35_IN_QUEUE.popleft()
              m3u8.close(id, begin_program_date_time)
            else: break

        if has_IDR:
          if PARTIAL_BEGIN_TIMESTAMP is not None:
            PART_DIFF = begin_timestamp - PARTIAL_BEGIN_TIMESTAMP
            if args.part_duration * ts.HZ < PART_DIFF:
              PARTIAL_BEGIN_TIMESTAMP = int(begin_timestamp - max(0, PART_DIFF - args.part_duration * ts.HZ))
              m3u8.continuousPartial(PARTIAL_BEGIN_TIMESTAMP, False)
          PARTIAL_BEGIN_TIMESTAMP = begin_timestamp
          m3u8.continuousSegment(PARTIAL_BEGIN_TIMESTAMP, True, begin_program_date_time)
        elif PARTIAL_BEGIN_TIMESTAMP is not None:
          PART_DIFF = begin_timestamp - PARTIAL_BEGIN_TIMESTAMP
          if args.part_duration * ts.HZ <= PART_DIFF:
            PARTIAL_BEGIN_TIMESTAMP = int(begin_timestamp - max(0, PART_DIFF - args.part_duration * ts.HZ))
            m3u8.continuousPartial(PARTIAL_BEGIN_TIMESTAMP)

        while (EMSG_FRAGMENTS): m3u8.push(EMSG_FRAGMENTS.popleft())
        while (H264_FRAGMENTS): m3u8.push(H264_FRAGMENTS.popleft())
        while (AAC_FRAGMENTS): m3u8.push(AAC_FRAGMENTS.popleft())

        if LATEST_VIDEO_TIMESTAMP_90KHZ is not None and LATEST_VIDEO_MONOTONIC_TIME is not None:
          TIMESTAMP_DIFF = (begin_timestamp - LATEST_VIDEO_TIMESTAMP_90KHZ) / ts.HZ
          TIME_DIFF = time.monotonic() - LATEST_VIDEO_MONOTONIC_TIME
          if args.input is not sys.stdin.buffer:
            SLEEP_BEGIN = time.monotonic()
            await asyncio.sleep(max(0, TIMESTAMP_DIFF - (TIME_DIFF + LATEST_VIDEO_SLEEP_DIFFERENCE)))
            SLEEP_END = time.monotonic()
            LATEST_VIDEO_SLEEP_DIFFERENCE = (SLEEP_END - SLEEP_BEGIN) - max(0, TIMESTAMP_DIFF - (TIME_DIFF + LATEST_VIDEO_SLEEP_DIFFERENCE))
        LATEST_VIDEO_TIMESTAMP_90KHZ = begin_timestamp
        LATEST_VIDEO_MONOTONIC_TIME = time.monotonic()

    elif PID == H265_PID:
      H265_PES_Parser.push(packet)
      for H265 in H265_PES_Parser:
        if LATEST_PCR_VALUE is None: continue
        if LATEST_PCR_DATETIME is None: continue
        dts = cast(int, H265.dts() if H265.has_dts() else H265.pts())
        pts = cast(int, H265.pts())
        cts = (pts - dts + ts.PCR_CYCLE) % ts.PCR_CYCLE
        timestamp = ((dts - LATEST_PCR_VALUE + ts.PCR_CYCLE) % ts.PCR_CYCLE) + LATEST_PCR_TIMESTAMP_90KHZ
        program_date_time: datetime = LATEST_PCR_DATETIME + timedelta(seconds=(((dts - LATEST_PCR_VALUE + ts.PCR_CYCLE) % ts.PCR_CYCLE) / ts.HZ))
        keyframe_in_samples = False
        samples: deque[bytes] = deque()

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
            keyframe_in_samples = True
            samples.append(ebsp)
          else:
            samples.append(ebsp)
        NEXT_H265 = (keyframe_in_samples, samples, timestamp, cts, program_date_time) if samples else None

        has_IDR = False
        begin_timestamp: int | None = None
        begin_program_date_time: datetime | None = None
        if CURR_H265:
          has_key_frame, samples, dts, cts, pdt = CURR_H265
          has_IDR = has_key_frame
          begin_timestamp = dts
          begin_program_date_time = pdt
          duration = timestamp - dts
          content = bytearray()
          while samples:
            ebsp = samples.popleft()
            content += len(ebsp).to_bytes(4, byteorder='big') + ebsp

          H265_FRAGMENTS.append(
            b''.join([
              moof(0,
                [
                  (1, duration, dts, 0, [(len(content), duration, has_key_frame, cts)])
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
              b''.join([
                hevcTrack(1, ts.HZ, VPS, SPS, PPS),
                mp4aTrack(2, ts.HZ, *AAC_CONFIG),
              ])
            )
          ]))
          INITIALIZATION_SEGMENT_DISPATCHED = True

        if begin_timestamp is None: continue

        if has_IDR and begin_program_date_time is not None:
          while SCTE35_OUT_QUEUE:
            if SCTE35_OUT_QUEUE[0][1] <= begin_program_date_time:
              id, start_date, end_date, attributes = SCTE35_OUT_QUEUE.popleft()
              m3u8.open(id, begin_program_date_time, end_date, **attributes)
            else: break
          while SCTE35_IN_QUEUE:
            if SCTE35_IN_QUEUE[0][1] <= begin_program_date_time:
              id, end_date = SCTE35_IN_QUEUE.popleft()
              m3u8.close(id, begin_program_date_time)
            else: break

        if has_IDR:
          if PARTIAL_BEGIN_TIMESTAMP is not None:
            PART_DIFF = begin_timestamp - PARTIAL_BEGIN_TIMESTAMP
            if args.part_duration * ts.HZ < PART_DIFF:
              PARTIAL_BEGIN_TIMESTAMP = int(begin_timestamp - max(0, PART_DIFF - args.part_duration * ts.HZ))
              m3u8.continuousPartial(PARTIAL_BEGIN_TIMESTAMP, False)
          PARTIAL_BEGIN_TIMESTAMP = begin_timestamp
          m3u8.continuousSegment(PARTIAL_BEGIN_TIMESTAMP, True, begin_program_date_time)
        elif PARTIAL_BEGIN_TIMESTAMP is not None:
          PART_DIFF = begin_timestamp - PARTIAL_BEGIN_TIMESTAMP
          if args.part_duration * ts.HZ <= PART_DIFF:
            PARTIAL_BEGIN_TIMESTAMP = int(begin_timestamp - max(0, PART_DIFF - args.part_duration * ts.HZ))
            m3u8.continuousPartial(PARTIAL_BEGIN_TIMESTAMP)

        while (EMSG_FRAGMENTS): m3u8.push(EMSG_FRAGMENTS.popleft())
        while (H265_FRAGMENTS): m3u8.push(H265_FRAGMENTS.popleft())
        while (AAC_FRAGMENTS): m3u8.push(AAC_FRAGMENTS.popleft())

        if LATEST_VIDEO_TIMESTAMP_90KHZ is not None and LATEST_VIDEO_MONOTONIC_TIME is not None:
          TIMESTAMP_DIFF = (begin_timestamp - LATEST_VIDEO_TIMESTAMP_90KHZ) / ts.HZ
          TIME_DIFF = time.monotonic() - LATEST_VIDEO_MONOTONIC_TIME
          if args.input is not sys.stdin.buffer:
            SLEEP_BEGIN = time.monotonic()
            await asyncio.sleep(max(0, TIMESTAMP_DIFF - (TIME_DIFF + LATEST_VIDEO_SLEEP_DIFFERENCE)))
            SLEEP_END = time.monotonic()
            LATEST_VIDEO_SLEEP_DIFFERENCE = (SLEEP_END - SLEEP_BEGIN) - max(0, TIMESTAMP_DIFF - (TIME_DIFF + LATEST_VIDEO_SLEEP_DIFFERENCE))
        LATEST_VIDEO_TIMESTAMP_90KHZ = begin_timestamp
        LATEST_VIDEO_MONOTONIC_TIME = time.monotonic()

    elif PID == AAC_PID:
      AAC_PES_Parser.push(packet)
      for AAC_PES in AAC_PES_Parser:
        if LATEST_PCR_VALUE is None: continue
        pts = cast(int, AAC_PES.pts())
        timestamp = ((pts - LATEST_PCR_VALUE + ts.PCR_CYCLE) % ts.PCR_CYCLE) + LATEST_PCR_TIMESTAMP_90KHZ
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

        PCR_PID = PMT.PCR_PID
        for stream_type, elementary_PID, _ in PMT:
          if stream_type == 0x1b:
            H264_PID = elementary_PID
          elif stream_type == 0x24:
            H265_PID = elementary_PID
          elif stream_type == 0x0F:
            AAC_PID = elementary_PID
          elif stream_type == 0x15:
            ID3_PID = elementary_PID
          elif stream_type == 0x86:
            SCTE35_PID = elementary_PID

    elif PID == ID3_PID:
      ID3_PES_Parser.push(packet)
      for ID3_PES in ID3_PES_Parser:
        if LATEST_PCR_VALUE is None: continue
        pts = cast(int, ID3_PES.pts())
        timestamp = ((pts - LATEST_PCR_VALUE + ts.PCR_CYCLE) % ts.PCR_CYCLE) + LATEST_PCR_TIMESTAMP_90KHZ
        ID3 = ID3_PES.PES_packet_data()
        EMSG_FRAGMENTS.append(emsg(ts.HZ, timestamp, None, 'https://aomedia.org/emsg/ID3', ID3))

    elif PID == SCTE35_PID:
      SCTE35_Parser.push(packet)
      for SCTE35 in SCTE35_Parser:
        if SCTE35.CRC32() != 0: continue

        if SCTE35.splice_command_type == SpliceInfoSection.SPLICE_INSERT:
          splice_insert: SpliceInsert = cast(SpliceInsert, SCTE35.splice_command)
          id = str(splice_insert.splice_event_id)
          if splice_insert.splice_event_cancel_indicator: continue
          if not splice_insert.program_splice_flag: continue
          if splice_insert.out_of_network_indicator:
            attributes = { 'SCTE35-OUT': '0x' + ''.join([f'{b:02X}' for b in SCTE35[:]]) }
            if splice_insert.splice_immediate_flag or not splice_insert.splice_time.time_specified_flag:
              if LATEST_PCR_DATETIME is None: continue
              start_date = LATEST_PCR_DATETIME

              if splice_insert.duration_flag:
                attributes['PLANNED-DURATION'] = str(splice_insert.break_duration.duration / ts.HZ)
                if splice_insert.break_duration.auto_return:
                  SCTE35_IN_QUEUE.append((id, start_date + timedelta(seconds=(splice_insert.break_duration.duration / ts.HZ))))
              SCTE35_OUT_QUEUE.append((id, start_date, None, attributes))
            else:
              if LATEST_PCR_VALUE is None: continue
              if LATEST_PCR_DATETIME is None: continue
              start_date = timedelta(seconds=(((cast(int, splice_insert.splice_time.pts_time) + SCTE35.pts_adjustment - LATEST_PCR_VALUE + ts.PCR_CYCLE) % ts.PCR_CYCLE) / ts.HZ)) + LATEST_PCR_DATETIME

              if splice_insert.duration_flag:
                attributes['PLANNED-DURATION'] = str(splice_insert.break_duration.duration / ts.HZ)
                if splice_insert.break_duration.auto_return:
                  SCTE35_IN_QUEUE.append((id, start_date + timedelta(seconds=(splice_insert.break_duration.duration / ts.HZ))))
              SCTE35_OUT_QUEUE.append((id, start_date, None, attributes))
          else:
            if splice_insert.splice_immediate_flag or not splice_insert.splice_time.time_specified_flag:
              if LATEST_PCR_DATETIME is None: continue
              end_date = LATEST_PCR_DATETIME
              SCTE35_IN_QUEUE.append((id, end_date))
            else:
              if LATEST_PCR_VALUE is None: continue
              if LATEST_PCR_DATETIME is None: continue
              end_date = timedelta(seconds=(((cast(int, splice_insert.splice_time.pts_time) + SCTE35.pts_adjustment - LATEST_PCR_VALUE + ts.PCR_CYCLE) % ts.PCR_CYCLE) / ts.HZ)) + LATEST_PCR_DATETIME
              SCTE35_IN_QUEUE.append((id, end_date))

        elif SCTE35.splice_command_type == SpliceInfoSection.TIME_SIGNAL:
          time_signal: TimeSignal = cast(TimeSignal, SCTE35.splice_command)
          if LATEST_PCR_VALUE is None: continue
          if LATEST_PCR_DATETIME is None: continue
          specified_time = LATEST_PCR_DATETIME
          if time_signal.splice_time.time_specified_flag:
            specified_time = timedelta(seconds=(((cast(int, time_signal.splice_time.pts_time) + SCTE35.pts_adjustment - LATEST_PCR_VALUE + ts.PCR_CYCLE) % ts.PCR_CYCLE) / ts.HZ)) + LATEST_PCR_DATETIME
          for descriptor in SCTE35.descriptors:
            if descriptor.descriptor_tag != 0x02: continue
            segmentation_descriptor: SegmentationDescriptor = cast(SegmentationDescriptor, descriptor)
            id = str(segmentation_descriptor.segmentation_event_id)
            if segmentation_descriptor.segmentation_event_cancel_indicator: continue
            if not segmentation_descriptor.program_segmentation_flag: continue

            if segmentation_descriptor.segmentation_event_id in SegmentationDescriptor.ADVERTISEMENT_BEGIN:
              attributes = { 'SCTE35-OUT': '0x' + ''.join([f'{b:02X}' for b in SCTE35[:]]) }
              if segmentation_descriptor.segmentation_duration_flag:
                attributes['PLANNED-DURATION'] = str(segmentation_descriptor.segmentation_duration / ts.HZ)
              SCTE35_OUT_QUEUE.append((id, specified_time, None, attributes))
            elif segmentation_descriptor.segmentation_type_id in SegmentationDescriptor.ADVERTISEMENT_END:
              SCTE35_IN_QUEUE.append((id, specified_time))

    else:
      pass

    if PID == PCR_PID and ts.has_pcr(packet):
      PCR_VALUE = (cast(int, ts.pcr(packet)) - ts.HZ + ts.PCR_CYCLE) % ts.PCR_CYCLE
      PCR_DIFF = ((PCR_VALUE - LATEST_PCR_VALUE + ts.PCR_CYCLE) % ts.PCR_CYCLE) if LATEST_PCR_VALUE is not None else 0
      LATEST_PCR_TIMESTAMP_90KHZ += PCR_DIFF
      if LATEST_PCR_DATETIME is None: LATEST_PCR_DATETIME = datetime.now(timezone.utc) - timedelta(seconds=(1))
      LATEST_PCR_DATETIME += timedelta(seconds=(PCR_DIFF / ts.HZ))
      LATEST_PCR_VALUE = PCR_VALUE

if __name__ == '__main__':
  asyncio.run(main())
