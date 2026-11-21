# ResiliNet

ResiliNet coordinates requests for relief resources and available inventory through email, while distinguishing recommendations from human-confirmed logistics activity.

## Language

**Incident**:
A relief-response coordination context with its own participants, reports, inventory, and public resource information.

**Sender**:
A person identified by the email address from which they submit reports or updates.

**Verified Sender**:
A Sender whose ownership of their email address has been established through passwordless verification. Verification does not grant authority over another participant's resources.

**Depot Coordinator**:
A Verified Sender assigned to manage inventory and confirm allocations for a particular depot within an Incident. This role does not extend to other depots or to changing delivery reports for another participant's Allocation.

**Entity Coordinator**:
A Verified Sender assigned to manage Need reports and delivery updates for a particular relief entity within an Incident. This role does not permit inventory snapshots or stock confirmation.

**Operator**:
A Verified Sender authorized to review escalations and override coordination decisions within an Incident.

**Need**:
A request for a quantity of a resource at an entity. Its quantity is either explicitly reported or clearly identified as an estimate.

**Supply**:
The latest reported on-hand quantity of a resource at an entity. Active reservations are considered separately when determining what remains available.

**Allocation**:
A proposed or confirmed assignment of a quantity of Supply to a Need. An Allocation is not evidence that goods have moved.

**Reservation**:
Stock held against a proposed or confirmed Allocation so that the same stock cannot be promised to competing Needs. A Reservation does not reduce reported on-hand stock.

**Proposal**:
An Allocation that reserves Supply but has not been confirmed by an authorized Depot Coordinator or Operator.

**Confirmed Allocation**:
An Allocation whose stock commitment has been accepted by an authorized Depot Coordinator or Operator. Confirmation does not mean dispatched or delivered.

**Delivered Allocation**:
An Allocation whose entire allocated quantity has been explicitly reported delivered by an authorized participant. A partially reported Allocation remains Confirmed until it is fully delivered or cancelled.

**Available Quantity**:
Reported on-hand Supply minus quantities held by active Reservations.

**Duplicate Need**:
A Need representing the same underlying request as an existing open Need, rather than an additional request for resources.

**Corroborating Report**:
A report linked to an existing Need as additional evidence of that Need. A report from a Sender without authority over the Need does not replace its authoritative quantity.

**Authoritative Report**:
A report from a Need's original Sender or an assigned entity representative that may revise that Need's details or status.

**Human Review**:
The state in which an Operator must assess a safety-sensitive, ambiguous, or otherwise escalated report before stock can be committed.

**Replay Mode**:
An offline demonstration of recorded inputs and model responses. Its results are identified as replayed rather than live service activity.

**Reference Code**:
The short public identifier used to refer to a Need or Allocation in email updates. `RN-` identifies a Need; `AL-` identifies one Allocation within a possibly split fulfilment.

**Action Link**:
A short-lived, single-use authorization to perform one kind of action for a particular participant within an Incident. It is distinct from a public reference code and from sender verification.

**Medical Escalation**:
A report containing safety-sensitive medical content that requires Human Review before stock can be committed. It may receive scheduling priority without receiving an automatic supply promise.
