---------------------------- MODULE BridgeDataQueue ----------------------------
(* One direction of the RTX Remix bridge: a writer process sends commands with
   data to a reader process through shared memory.  Model of:

     bridge/src/util/util_bridgecommand.cpp   Bridge::syncDataQueue, Command::Command, ~Command
     bridge/src/util/util_bridgecommand.h     Command::send_data, Bridge::get_data
     bridge/src/util/util_circularqueue.h     CircularQueue::push / pop   (data queue positions)
     bridge/src/util/util_circularbuffer.h    CircularBuffer::push(size, obj), pull(obj), ensure_space
     bridge/src/util/util_atomiccircularqueue.h  push(obj, batching), flush_staged   (command queue)
     bridge/src/server/main.cpp               ProcessDeviceCommandQueue: the block after the switch
                                              that stores serverDataPos and releases dataSemaphore

   The same code runs on four channels.  The .cfg files pick one each:
     device.cfg  game -> server device commands   (writer may wait, reader publishes its position)
     reply.cfg   server -> game replies, and both module channels (writer never waits)

   Not modelled: timeouts (a wait that could only end by timing out is the bug, see NoFreeze),
   the contents of the data, the reply the game waits for after some commands.               *)
EXTENDS Integers, Sequences, FiniteSets

CONSTANTS
  N,                \* data queue size in slots                  CircularQueue::m_size
  MaxPayload,       \* largest blob payload in slots (< N - 1)
  MaxItems,         \* most data items in one command
  MaxCmdSlots,      \* most data slots one command may use (keeps commands far smaller than N)
  MaxQueued,        \* command queue capacity                    AtomicCircularQueue::m_queueSize
  MaxStaged,        \* AtomicCircularQueue::kMaxStagedElements
  WriterMayWait,    \* syncDataQueue: readerPublishesPosition
  ReaderPublishes,  \* the reader is ProcessDeviceCommandQueue (stores serverDataPos, releases dataSemaphore)
  StageSetters,     \* ~Command: batchSetter can be true (game side, device channel only)
  FlushBeforeWait,  \* handleOverwriteCondition calls commands->flush_staged() before it waits
  UidFirst,         \* Command::Command pushes the command UID as first data item (game side only)
  SyncMatchesRollover, \* FALSE = the code as it is.  TRUE = proposed fix for finding F1: syncDataQueue counts a
                    \*   blob that ends exactly on the last slot as wrapping, as ensure_space does
  RequestResponse,  \* protocol assumption: a command is only started after the reader finished the previous one
  HandshakeFirst    \* protocol assumption (device channel): the first command is CreateDevice, which is not a
                    \*   staged setter, and the game sends nothing more until the server finished it
                    \*   (client/d3d9_device_base.cpp waits for its reply)

VARIABLES
  wpos,        \* writer's data position                       s_pWriterChannel->data->m_pos
  batchStart,  \* s_curBatchStartPos (-1 outside a command)
  wpc,         \* writer: "idle", "cmd" (inside a Command), "wait1"/"wait2" (blocked on dataSemaphore in
               \*   handleOverwriteCondition: 1 = double overflow branch, 2 = overwrite branch),
               \*   "ending" (~Command blocked on a full command queue), "fatal" (out-of-buffer message box)
  pend,        \* the data item the blocked writer is about to push; batched flag while "ending"
  cur,         \* the command being written
  stg,         \* command headers staged in the command queue, not yet visible to the reader
  pub,         \* command headers visible to the reader
  rpos,        \* reader's data position
  rpc,         \* reader: "idle" or "publish" (data of rcur pulled, command being executed)
  rcur,        \* the command the reader is executing
  sdp,         \* *serverDataPos            (-1 until the reader first publishes)
  cep,         \* *clientDataExpectedPos    (-1 = no overwrite condition)
  rrp,         \* *serverResetPosRequired
  sem,         \* dataSemaphore count (0 or 1)
  corrupt      \* the writer wrote a slot the reader had not finished with

vars == <<wpos, batchStart, wpc, pend, cur, stg, pub, rpos, rpc, rcur, sdp, cep, rrp, sem, corrupt>>

Slots == 0 .. N - 1
\* A data item is 0 (one value: send_data(obj)) or k > 0 (a blob with k payload slots: send_data(size, obj)).
Items == 0 .. MaxPayload
NoCmd == [items |-> <<>>, slots |-> {}, endPos |-> -1]

----------------------------------------------------------------------------
(* Data queue positions: CircularQueue::pop and CircularBuffer::ensure_space. *)
Inc(p) == IF p + 1 < N THEN p + 1 ELSE 0
\* A blob writes its size at p, then its payload at the next position, or at 0 when the payload
\* does not fit before the end ("if (m_pos + space_needed >= m_size) m_pos = 0").
BlobStart(p, k) == IF Inc(p) + k >= N THEN 0 ELSE Inc(p)
ItemSlots(p, it) == IF it = 0 THEN {p} ELSE {p} \cup {BlobStart(p, it) + i : i \in 0 .. it - 1}
ItemEnd(p, it)   == IF it = 0 THEN Inc(p) ELSE BlobStart(p, it) + it

\* Reader side: position after pulling all items, and whether any pull moved the position backwards
\* (Bridge::get_data then clears serverResetPosRequired).
RECURSIVE Walk(_, _)
Walk(p, items) ==
  IF items = <<>> THEN <<p, FALSE>>
  ELSE LET q == ItemEnd(p, Head(items))
           r == Walk(q, Tail(items))
       IN <<r[1], (q < p) \/ r[2]>>

RECURSIVE SlotsUsed(_)
SlotsUsed(items) == IF items = <<>> THEN 0 ELSE (IF Head(items) = 0 THEN 1 ELSE Head(items) + 1) + SlotsUsed(Tail(items))

RECURSIVE AllSlots(_)
AllSlots(cmds) == IF cmds = <<>> THEN {} ELSE Head(cmds).slots \cup AllSlots(Tail(cmds))
\* Slots holding data the reader has not finished with (it frees a command's data when it finishes the command).
Live == cur.slots \cup AllSlots(stg) \cup AllSlots(pub) \cup rcur.slots

----------------------------------------------------------------------------
(* Bridge::syncDataQueue(expectedMemUsage, posResetOnLastIndex), evaluated at the writer's position. *)
Need(it)  == IF it = 0 THEN 1 ELSE it + 1              \* expectedMemUsage
Exp0(it)  == wpos + Need(it) - 1                       \* expectedClientDataPos
Wraps(it) == Exp0(it) + (IF SyncMatchesRollover /\ it # 0 THEN 1 ELSE 0) >= N    \* wrapsBuffer
Exp(it)   == IF Wraps(it) THEN (IF it # 0 THEN Need(it) - 1 ELSE Exp0(it) - N) ELSE Exp0(it)
\* "if (initialUnreadWrap || activeOverwrite)" with serverCount = sc
Overwrites(it, sc) ==
  \/ sc < 0 /\ Wraps(it)
  \/ /\ sc >= 0
     /\ Exp(it) >= sc
     /\ \/ batchStart < sc
        \/ batchStart > sc /\ Exp(it) < batchStart
        \/ batchStart <= sc /\ Wraps(it)
\* handleOverwriteCondition: "not enough space to ever succeed" -> errLogMessageBoxAndExit
TooBig(it) == Need(it) + (IF wpos >= batchStart THEN wpos - batchStart ELSE wpos + N - batchStart) > N

\* The push that follows syncDataQueue.
Push(it) ==
  /\ corrupt' = (corrupt \/ (ItemSlots(wpos, it) \cap Live # {}))
  /\ cur' = [cur EXCEPT !.items = Append(@, it), !.slots = @ \cup ItemSlots(wpos, it)]
  /\ wpos' = ItemEnd(wpos, it)

\* handleOverwriteCondition up to the semaphore wait.
EnterWait(kind, it) ==
  /\ pub' = IF FlushBeforeWait THEN pub \o stg ELSE pub
  /\ stg' = IF FlushBeforeWait THEN <<>> ELSE stg
  /\ cep' = IF batchStart > 0 THEN batchStart - 1 ELSE 0
  /\ wpc' = IF TooBig(it) THEN "fatal" ELSE kind
  /\ pend' = it
  /\ UNCHANGED <<wpos, batchStart, cur, corrupt>>

CanSend(it) ==
  /\ wpc = "cmd"
  /\ Len(cur.items) < MaxItems
  /\ SlotsUsed(cur.items) + Need(it) <= MaxCmdSlots
  /\ (UidFirst /\ cur.items = <<>>) => it = 0

----------------------------------------------------------------------------
(* Writer *)
BeginCmd ==                                            \* Command::Command
  /\ wpc = "idle"
  /\ RequestResponse => (pub = <<>> /\ stg = <<>> /\ rpc = "idle")
  /\ HandshakeFirst => (sdp >= 0 \/ (wpos = 0 /\ pub = <<>> /\ stg = <<>> /\ rpc = "idle"))
  /\ wpc' = "cmd" /\ batchStart' = wpos
  /\ cur' = [items |-> <<>>, slots |-> {}, endPos |-> -1]
  /\ UNCHANGED <<wpos, pend, stg, pub, rpos, rpc, rcur, sdp, cep, rrp, sem, corrupt>>

Send(it) ==                                            \* send_data: syncDataQueue found nothing to wait for
  /\ CanSend(it)
  /\ \/ ~WriterMayWait
     \/ ~(Wraps(it) /\ rrp) /\ ~Overwrites(it, sdp)
  /\ rrp' = IF WriterMayWait /\ Wraps(it) THEN TRUE ELSE rrp
  /\ Push(it)
  /\ UNCHANGED <<batchStart, wpc, pend, stg, pub, rpos, rpc, rcur, sdp, cep, sem>>

SendDoubleOverflow(it) ==                              \* "Double Overflow Condition Detected"
  /\ CanSend(it) /\ WriterMayWait
  /\ Wraps(it) /\ rrp
  /\ EnterWait("wait1", it)
  /\ UNCHANGED <<rpos, rpc, rcur, sdp, rrp, sem>>

SendOverwrite(it) ==                                   \* "if (initialUnreadWrap || activeOverwrite)"
  /\ CanSend(it) /\ WriterMayWait
  /\ ~(Wraps(it) /\ rrp) /\ Overwrites(it, sdp)
  /\ EnterWait("wait2", it)
  /\ UNCHANGED <<rpos, rpc, rcur, sdp, rrp, sem>>

Wake1 ==                                               \* woken in the double overflow branch; serverCount is re-read
  /\ wpc = "wait1" /\ sem = 1
  /\ sem' = 0
  /\ IF Overwrites(pend, sdp)
       THEN /\ EnterWait("wait2", pend)
            /\ rrp' = FALSE
       ELSE /\ cep' = -1
            /\ rrp' = Wraps(pend)
            /\ Push(pend)
            /\ wpc' = "cmd"
            /\ UNCHANGED <<batchStart, pend, stg, pub>>
  /\ UNCHANGED <<rpos, rpc, rcur, sdp>>

Wake2 ==                                               \* woken in the overwrite branch
  /\ wpc = "wait2" /\ sem = 1
  /\ sem' = 0 /\ cep' = -1
  /\ rrp' = Wraps(pend)
  /\ Push(pend)
  /\ wpc' = "cmd"
  /\ UNCHANGED <<batchStart, pend, stg, pub, rpos, rpc, rcur, sdp>>

\* Command::~Command -> commands->push(header, batchSetter)
PushHeader(batched) ==
  LET rec  == [cur EXCEPT !.endPos = wpos]
      stg2 == Append(stg, rec)
  IN /\ IF ~batched \/ Len(stg2) >= MaxStaged
          THEN pub' = pub \o stg2 /\ stg' = <<>>
          ELSE stg' = stg2 /\ pub' = pub
     /\ wpc' = "idle" /\ batchStart' = -1 /\ cur' = NoCmd

EndCmd(batched) ==
  /\ wpc = "cmd"
  /\ UidFirst => cur.items # <<>>
  /\ batched => (StageSetters /\ (HandshakeFirst => sdp >= 0))
  /\ IF Len(pub) + Len(stg) < MaxQueued
       THEN PushHeader(batched) /\ UNCHANGED pend
       ELSE \* "Queue full: the consumer can only make room if it can see the staged elements."
            /\ pub' = pub \o stg /\ stg' = <<>>
            /\ wpc' = "ending" /\ pend' = IF batched THEN 1 ELSE 0
            /\ UNCHANGED <<batchStart, cur>>
  /\ UNCHANGED <<wpos, rpos, rpc, rcur, sdp, cep, rrp, sem, corrupt>>

EndRetry ==
  /\ wpc = "ending"
  /\ Len(pub) + Len(stg) < MaxQueued
  /\ PushHeader(pend = 1)
  /\ UNCHANGED <<wpos, pend, rpos, rpc, rcur, sdp, cep, rrp, sem, corrupt>>

----------------------------------------------------------------------------
(* Reader *)
RPull ==                                               \* pop_front, then get_data for every item of the command
  /\ rpc = "idle" /\ pub # <<>>
  /\ LET c == Head(pub)
         w == Walk(rpos, c.items)
     IN /\ rcur' = c
        /\ rpos' = w[1]
        /\ rrp' = IF w[2] THEN FALSE ELSE rrp
  /\ pub' = Tail(pub)
  /\ rpc' = "publish"
  /\ UNCHANGED <<wpos, batchStart, wpc, pend, cur, stg, sdp, cep, sem, corrupt>>

RPublish ==                                            \* the command is done; ProcessDeviceCommandQueue publishes
  /\ rpc = "publish"
  /\ rpc' = "idle" /\ rcur' = NoCmd
  /\ IF ReaderPublishes
       THEN /\ sdp' = rpos
            /\ IF cep # -1 /\ rpos > cep /\ ~rrp
                 THEN sem' = 1 /\ cep' = -1
                 ELSE UNCHANGED <<sem, cep>>
       ELSE UNCHANGED <<sdp, sem, cep>>
  /\ UNCHANGED <<wpos, batchStart, wpc, pend, cur, stg, pub, rpos, rrp, corrupt>>

Halt == wpc = "fatal" /\ UNCHANGED vars                \* the process showed the message box and exited

Next ==
  \/ BeginCmd
  \/ \E it \in Items : Send(it) \/ SendDoubleOverflow(it) \/ SendOverwrite(it)
  \/ Wake1 \/ Wake2
  \/ \E b \in BOOLEAN : EndCmd(b)
  \/ EndRetry
  \/ RPull \/ RPublish
  \/ Halt

Init ==
  /\ wpos = 0 /\ batchStart = -1 /\ wpc = "idle" /\ pend = 0 /\ cur = NoCmd
  /\ stg = <<>> /\ pub = <<>>
  /\ rpos = 0 /\ rpc = "idle" /\ rcur = NoCmd
  /\ sdp = -1 /\ cep = -1 /\ rrp = FALSE /\ sem = 0
  /\ corrupt = FALSE

Spec == Init /\ [][Next]_vars

----------------------------------------------------------------------------
(* Properties *)

\* The game never freezes in the bridge: a writer never waits on dataSemaphore when the reader has
\* nothing left to do, because then nothing can ever release it (in the code: a 300 s stall, then
\* "Max retries reached" and a desynchronised bridge).
NoFreeze == ~(wpc \in {"wait1", "wait2"} /\ sem = 0 /\ pub = <<>> /\ rpc = "idle")

\* The writer never overwrites data the reader has not finished with (in the code: the server would
\* execute a draw call with another command's bytes).
NoOverwrite == ~corrupt

\* After reading a command's data the reader is where the writer was when it sent the header
\* (in the code: CHECK_DATA_OFFSET, "Data not in sync").
InSync == rpc = "publish" => rpos = rcur.endPos

\* A command far smaller than the queue never triggers the "out of buffer memory" exit.
NoFatal == wpc # "fatal"

=============================================================================
