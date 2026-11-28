import streamlit as st
import psycopg2
from datetime import date

# Browser tab title and a wide layout so the event list and forms have room
st.set_page_config(page_title="Event Booking System", layout="wide")


# ===================== MANAGER ACCOUNT =====================
# Customers never log in; only the manager signs in (from the sidebar) to insert, update and delete data.
# The manager's email and password are kept in .streamlit/secrets.toml (not in GitHub), like the database URL.
MANAGER_EMAIL = st.secrets["MANAGER_EMAIL"].strip().lower()
MANAGER_PASSWORD = st.secrets["MANAGER_PASSWORD"]


# ===================== DATABASE HELPERS =====================
# Connect to the Supabase Postgres database using the connection string saved in .streamlit/secrets.toml
def get_connection():
    return psycopg2.connect(st.secrets["DB_URL"])


# Run a SELECT query and return the rows as a list of dictionaries (so Streamlit can show them as a table)
def read_data(sql, params=None):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        columns = [col[0] for col in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        conn.close()


# Run an INSERT / UPDATE / DELETE query, save the change, and show an error message if the database rejects it
def write_data(sql, params):
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        return True
    except Exception as e:
        st.error(f"Database error: {e}")
        return False
    finally:
        if conn:
            conn.close()


# ===================== SESSION STATE =====================
# Remember between reruns: manager sign-in, the last booking confirmation, the booking being looked up, and a one-time message
for state_key, default in {"is_manager": False, "confirmation": None, "lookup": None, "flash": None}.items():
    if state_key not in st.session_state:
        st.session_state[state_key] = default


# Show a success message saved before the last rerun (e.g. "Booking cancelled"), then forget it
def show_flash():
    if st.session_state["flash"]:
        st.success(st.session_state["flash"])
        st.session_state["flash"] = None


# ===================== SIDEBAR: MANAGER LOGIN / LOGOUT =====================
# Show the manager login form, or (once signed in) who is signed in and a log-out button
with st.sidebar:
    if st.session_state["is_manager"]:
        st.success(f"Signed in as manager: {MANAGER_EMAIL}")
        if st.button("Log out"):
            st.session_state["is_manager"] = False
            st.rerun()
    else:
        st.subheader("Manager login")
        st.caption("Customers don't need to log in. This is only for staff.")
        with st.form("manager_login_form"):
            login_email = st.text_input("Email")
            login_password = st.text_input("Password", type="password")
            if st.form_submit_button("Log in"):
                if login_email.strip().lower() == MANAGER_EMAIL and login_password == MANAGER_PASSWORD:
                    st.session_state["is_manager"] = True
                    st.rerun()
                else:
                    st.error("Invalid email or password.")


# ===================== STATUS CODES =====================
# Status codes the website writes. Change these if your tables use different codes.
CASH_METHOD = "CASH"  # paid later at the box office
BOOKING_STATUS = "PENDING"  # the trigger trg_payment_confirm_booking changes it to CONFIRMED when a payment is COMPLETED
CONFIRMED_STATUS = "CONFIRMED"
CANCELLED_STATUS = "CANCELLED"
SEAT_HOLDING_STATUSES = ["CONFIRMED", "PENDING"]  # bookings with these statuses use up seats
PAYMENT_STATUS = "COMPLETED"  # card / PayPal / e-Transfer are paid right away
CASH_PAYMENT_STATUS = "PENDING"  # cash stays PENDING (and the booking stays PENDING) until paid at the box office
REFUNDED_PAYMENT_STATUS = "REFUNDED"  # a paid booking that the customer cancels
CANCELLED_PAYMENT_STATUS = "CANCELLED"  # an unpaid cash booking that the customer cancels
TICKET_STATUS = "VALID"
CANCELLED_TICKET_STATUS = "CANCELLED"
MAX_TICKETS_PER_BOOKING = 20


# Seats still free for one event = venue capacity minus seats held by CONFIRMED and PENDING bookings
def seats_left(cur, event_id):
    cur.execute(
        "SELECT v.venue_seat_capacity - COALESCE((SELECT SUM(b.booking_seat_count) FROM customer_bookings b "
        "WHERE b.event_id = e.event_id AND b.booking_status_code = ANY(%s)), 0) "
        "FROM events e JOIN venues v ON v.venue_id = e.venue_id WHERE e.event_id = %s",
        (SEAT_HOLDING_STATUSES, event_id),
    )
    return cur.fetchone()[0]


# Create or update the customer, then the booking, payment and tickets in ONE transaction.
# If any step fails, nothing is saved. Returns the confirmation details, or None on failure.
def create_booking(name, phone, email, address, event_id, ticket_count, method):
    conn = get_connection()
    try:
        cur = conn.cursor()

        # Lock the event row so two people can't grab the last seats at the same moment, and check it is still upcoming
        cur.execute("SELECT ticket_price, event_start_date > NOW() FROM events WHERE event_id = %s FOR UPDATE", (event_id,))
        price, is_upcoming = cur.fetchone()
        if not is_upcoming:
            raise ValueError("This event has already started.")

        # Make sure there are still enough free seats
        available = seats_left(cur, event_id)
        if ticket_count > available:
            raise ValueError(f"Only {available} seat(s) left for this event." if available > 0 else "This event is sold out.")

        # Work out the amount from the event's ticket price (done in the database so it can't be changed in the browser)
        amount = price * ticket_count

        # Existing email: update that customer's details. New email: create a new customer.
        cur.execute("SELECT customer_id FROM customers WHERE LOWER(customer_email) = LOWER(%s)", (email,))
        row = cur.fetchone()
        if row:
            customer_id = row[0]
            cur.execute(
                "UPDATE customers SET customer_name=%s, customer_phone=%s, customer_address=%s, customer_payment_method=%s "
                "WHERE customer_id=%s",
                (name, phone, address, method, customer_id),
            )
        else:
            cur.execute(
                "INSERT INTO customers (customer_name, customer_phone, customer_email, customer_address, customer_payment_method) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING customer_id",
                (name, phone, email, address, method),
            )
            customer_id = cur.fetchone()[0]

        # Booking (today's date and the event's start time are filled in automatically)
        cur.execute(
            "INSERT INTO customer_bookings (customer_id, event_id, booking_status_code, event_datetime, booking_made_date, booking_seat_count) "
            "VALUES (%s, %s, %s, (SELECT event_start_date FROM events WHERE event_id = %s), CURRENT_DATE, %s) RETURNING booking_id",
            (customer_id, event_id, BOOKING_STATUS, event_id, ticket_count),
        )
        booking_id = cur.fetchone()[0]

        # Payment record; the trigger trg_payment_set_reference gives completed payments the next TXN- reference
        payment_status = CASH_PAYMENT_STATUS if method == CASH_METHOD else PAYMENT_STATUS
        cur.execute(
            "INSERT INTO payments (booking_id, payment_method_code, payment_amount, payment_date, payment_status) "
            "VALUES (%s, %s, %s, NOW(), %s) RETURNING payment_status, transaction_reference",
            (booking_id, method, amount, payment_status),
        )
        saved_payment_status, reference = cur.fetchone()

        # Read back the booking status (the payment trigger may have changed PENDING to CONFIRMED)
        cur.execute("SELECT booking_status_code FROM customer_bookings WHERE booking_id = %s", (booking_id,))
        booking_status = cur.fetchone()[0]

        # Card / PayPal / e-Transfer bookings are already CONFIRMED, so trg_booking_status_tickets
        # has created their tickets. Cash bookings are still PENDING, so the app creates them here.
        cur.execute("SELECT ticket_code FROM tickets WHERE booking_id = %s ORDER BY ticket_code", (booking_id,))
        ticket_codes = [row[0] for row in cur.fetchall()]
        if not ticket_codes:
            for n in range(1, ticket_count + 1):
                code = f"TKT-E{event_id:02d}-B{booking_id:03d}-{n:02d}"
                cur.execute(
                    "INSERT INTO tickets (booking_id, ticket_code, ticket_status, ticket_issue_date) VALUES (%s, %s, %s, NOW())",
                    (booking_id, code, TICKET_STATUS),
                )
                ticket_codes.append(code)

        conn.commit()
        return {
            "booking_id": booking_id,
            "booking_status": booking_status,
            "payment_status": saved_payment_status,
            "reference": reference,
            "amount": amount,
            "ticket_codes": ticket_codes,
        }
    except Exception as e:
        conn.rollback()
        st.error(f"Booking failed, nothing was saved: {e}")
        return None
    finally:
        conn.close()


# Find a booking by its ID and the customer's email (both must match), with its event, payments and tickets
def lookup_booking(booking_id, email):
    found = read_data(
        "SELECT b.booking_id, b.booking_status_code, b.booking_seat_count, b.booking_made_date, "
        "c.customer_name, c.customer_email, e.event_name, e.event_start_date, v.venue_name, "
        "e.event_start_date > NOW() AS is_upcoming "
        "FROM customer_bookings b "
        "JOIN customers c ON c.customer_id = b.customer_id "
        "JOIN events e ON e.event_id = b.event_id "
        "JOIN venues v ON v.venue_id = e.venue_id "
        "WHERE b.booking_id = %s AND LOWER(c.customer_email) = LOWER(%s)",
        (booking_id, email),
    )
    if not found:
        return None
    booking = found[0]
    booking["payments"] = read_data(
        "SELECT p.payment_date, m.payment_method_description, p.payment_amount, p.payment_status, p.transaction_reference "
        "FROM payments p JOIN ref_payment_methods m ON m.payment_method_code = p.payment_method_code "
        "WHERE p.booking_id = %s ORDER BY p.payment_date",
        (booking_id,),
    )
    booking["tickets"] = read_data(
        "SELECT ticket_code, ticket_status FROM tickets WHERE booking_id = %s ORDER BY ticket_code", (booking_id,)
    )
    return booking


# Cancel a booking in ONE transaction: paid payments are refunded, unpaid cash is cancelled, tickets are cancelled
# (the seats are freed automatically because CANCELLED bookings don't count toward seats used). Returns the refund amount.
def cancel_booking(booking_id, email):
    conn = get_connection()
    try:
        cur = conn.cursor()

        # Check again that the booking belongs to this email, can still be cancelled, and the event hasn't started
        cur.execute(
            "SELECT b.booking_status_code, e.event_start_date > NOW() FROM customer_bookings b "
            "JOIN customers c ON c.customer_id = b.customer_id JOIN events e ON e.event_id = b.event_id "
            "WHERE b.booking_id = %s AND LOWER(c.customer_email) = LOWER(%s) FOR UPDATE OF b",
            (booking_id, email),
        )
        row = cur.fetchone()
        if row is None or row[0] not in SEAT_HOLDING_STATUSES or not row[1]:
            raise ValueError("This booking can't be cancelled.")

        # Refund money that was paid; cancel cash that was never paid
        cur.execute(
            "UPDATE payments SET payment_status = CASE WHEN payment_status = %s THEN %s ELSE %s END "
            "WHERE booking_id = %s AND payment_status IN (%s, %s) RETURNING payment_status, payment_amount",
            (PAYMENT_STATUS, REFUNDED_PAYMENT_STATUS, CANCELLED_PAYMENT_STATUS, booking_id, PAYMENT_STATUS, CASH_PAYMENT_STATUS),
        )
        refunded = sum(amount for status, amount in cur.fetchall() if status == REFUNDED_PAYMENT_STATUS)

        # Cancel the booking and its tickets
        cur.execute("UPDATE customer_bookings SET booking_status_code = %s WHERE booking_id = %s", (CANCELLED_STATUS, booking_id))
        cur.execute("UPDATE tickets SET ticket_status = %s WHERE booking_id = %s", (CANCELLED_TICKET_STATUS, booking_id))

        conn.commit()
        return refunded
    except Exception as e:
        conn.rollback()
        st.error(f"Cancellation failed, nothing was changed: {e}")
        return None
    finally:
        conn.close()


# ===================== CUSTOMER PAGE (what everyone sees when the site opens) =====================
if not st.session_state["is_manager"]:
    st.title("Event Booking System")
    show_flash()
    confirmation = st.session_state["confirmation"]

    # ---------- Confirmation screen shown right after a successful booking ----------
    if confirmation:
        # Paid now (card / PayPal / e-Transfer) vs. pay at the box office (cash)
        if confirmation["payment_status"] == PAYMENT_STATUS:
            st.success(f"Payment received. Your booking is {confirmation['booking_status']}.")
        else:
            st.warning(
                "Your booking is reserved, but your tickets are NOT fully active yet. "
                "Pay at the box office before the event to activate them."
            )

        # Booking, payment and ticket details
        st.subheader(f"Booking ID: {confirmation['booking_id']}")
        st.write(f"**Event:** {confirmation['event']}")
        st.write(f"**Name:** {confirmation['name']}")
        st.write(f"**Email:** {confirmation['email']}")
        st.write(f"**Booking status:** {confirmation['booking_status']}")
        st.write(f"**Payment:** ${confirmation['amount']:.2f} by {confirmation['method']} ({confirmation['payment_status']})")
        if confirmation["reference"]:
            st.write(f"**Transaction reference:** {confirmation['reference']}")
        st.write(f"**Tickets issued:** {len(confirmation['ticket_codes'])}")
        st.write("**Your ticket number(s):**")
        for code in confirmation["ticket_codes"]:
            st.code(code, language=None)
        st.caption("Keep your booking ID and email. You can use them below the booking form to look up or cancel this booking.")

        # Clear the confirmation and go back to the event list and booking form
        if st.button("Make another booking"):
            st.session_state["confirmation"] = None
            st.rerun()
        st.stop()

    # ---------- Upcoming events with venue, type, organizer, price and seats left, in one scrollable table ----------
    st.header("Upcoming events and venues")
    upcoming = read_data(
        "SELECT e.event_id, e.event_name, t.event_type_description, e.event_start_date, e.event_end_date, "
        "v.venue_name, v.venue_seat_capacity, o.organizer_name, e.ticket_price, "
        "v.venue_seat_capacity - COALESCE(SUM(b.booking_seat_count) FILTER (WHERE b.booking_status_code = ANY(%s)), 0) AS seats_left "
        "FROM events e "
        "JOIN venues v ON v.venue_id = e.venue_id "
        "JOIN ref_event_types t ON t.event_type_code = e.event_type_code "
        "JOIN event_organizers o ON o.organizer_id = e.organizer_id "
        "LEFT JOIN customer_bookings b ON b.event_id = e.event_id "
        "WHERE e.event_start_date > NOW() "
        "GROUP BY e.event_id, t.event_type_description, v.venue_name, v.venue_seat_capacity, o.organizer_name "
        "ORDER BY e.event_start_date",
        (SEAT_HOLDING_STATUSES,),
    )

    if not upcoming:
        st.info("There are no upcoming events right now. Please check back later.")
    else:
        # Friendly column names for the table (the event id is only used internally)
        st.dataframe(
            [
                {
                    "Event": e["event_name"],
                    "Type": e["event_type_description"],
                    "Starts": e["event_start_date"],
                    "Ends": e["event_end_date"],
                    "Venue": e["venue_name"],
                    "Organizer": e["organizer_name"],
                    "Ticket price ($)": e["ticket_price"],
                    "Seats left": "Sold out" if e["seats_left"] <= 0 else f"{e['seats_left']} of {e['venue_seat_capacity']}",
                }
                for e in upcoming
            ],
            width="stretch",
            hide_index=True,
        )

    # ---------- Booking form (only priced events with seats left can be booked) ----------
    st.header("Book tickets")
    bookable = {e["event_id"]: e for e in upcoming if e["ticket_price"] is not None and e["seats_left"] > 0}
    methods = {
        r["payment_method_code"]: r["payment_method_description"]
        for r in read_data(
            "SELECT payment_method_code, payment_method_description FROM ref_payment_methods "
            "ORDER BY payment_method_code = %s, payment_method_code",  # cash is listed last so it isn't picked by default
            (CASH_METHOD,),
        )
    }

    if not bookable:
        st.info("There are no events with tickets available right now.")
    else:
        # Event label shown in the dropdown, e.g. "Holiday Jazz Night – Dec 12, 2026 08:00 PM at Prairie Concert Hall ($65.00 each, 48 seats left)"
        def event_label(event_id):
            e = bookable[event_id]
            return (
                f"{e['event_name']} – {e['event_start_date']:%b %d, %Y %I:%M %p} at {e['venue_name']} "
                f"(${e['ticket_price']:.2f} each, {e['seats_left']} seats left)"
            )

        # Customer details on the left, booking and payment choices on the right
        left, right = st.columns(2)
        with left:
            st.markdown("**Your details**")
            name = st.text_input("Name", key="book_name")
            phone = st.text_input("Phone", key="book_phone")
            email = st.text_input("Email", key="book_email")
            address = st.text_input("Address", key="book_address")
        with right:
            st.markdown("**Booking and payment**")
            eid = st.selectbox("Event", list(bookable.keys()), format_func=event_label, key="book_event")

            # Number of tickets (the limit stays fixed so the number never changes by itself; seats are checked on booking)
            tickets = int(st.number_input("Number of tickets", min_value=1, max_value=MAX_TICKETS_PER_BOOKING, value=1, step=1, key="book_tickets"))
            st.caption(f"{bookable[eid]['seats_left']} seat(s) left for this event, up to {MAX_TICKETS_PER_BOOKING} per booking.")
            method = st.selectbox("Payment type", list(methods.keys()), format_func=lambda m: methods[m], key="book_method")

            # Live total (price x tickets) so the customer sees what they will pay before booking
            total = bookable[eid]["ticket_price"] * tickets
            st.metric("Total", f"${total:.2f}")

        # Warn cash customers before they book that their tickets won't be fully active until they pay
        if method == CASH_METHOD:
            st.warning(
                "Pay at the box office: your tickets will be issued now but will NOT be fully active "
                "(booking stays PENDING) until you pay at the box office."
            )

        # Check the required fields, save everything in one transaction, then show the confirmation screen
        if st.button("Book and pay", type="primary"):
            if not all(v.strip() for v in (name, phone, email, address)):
                st.warning("Name, phone, email and address are required.")
            elif tickets > bookable[eid]["seats_left"]:
                st.warning(f"Only {bookable[eid]['seats_left']} seat(s) are left for this event. Please choose fewer tickets.")
            else:
                result = create_booking(name.strip(), phone.strip(), email.strip(), address.strip(), eid, tickets, method)
                if result:
                    result.update({
                        "event": event_label(eid),
                        "name": name.strip(),
                        "email": email.strip(),
                        "method": methods[method],
                    })
                    st.session_state["confirmation"] = result
                    st.rerun()

    # ---------- Look up or cancel an existing booking (booking ID + email) ----------
    st.header("Find or cancel a booking")
    find_left, find_right = st.columns(2)
    with find_left:
        find_id = st.number_input("Booking ID", min_value=1, step=1, value=None, key="find_id")
    with find_right:
        find_email = st.text_input("Email used for the booking", key="find_email")

    # Remember which booking to show so it stays on screen after the next click
    if st.button("Find booking"):
        if find_id is None or not find_email.strip():
            st.warning("Enter both the booking ID and the email.")
        else:
            st.session_state["lookup"] = (int(find_id), find_email.strip())

    if st.session_state["lookup"]:
        lookup_id, lookup_email = st.session_state["lookup"]
        booking = lookup_booking(lookup_id, lookup_email)

        if booking is None:
            st.error("No booking matches that booking ID and email.")
        else:
            # Booking summary
            st.subheader(f"Booking {booking['booking_id']}: {booking['booking_status_code']}")
            st.write(f"**Event:** {booking['event_name']} – {booking['event_start_date']:%b %d, %Y %I:%M %p} at {booking['venue_name']}")
            st.write(f"**Name:** {booking['customer_name']}")
            st.write(f"**Seats:** {booking['booking_seat_count']} (booked on {booking['booking_made_date']})")

            # Payments for this booking
            st.markdown("**Payments**")
            st.dataframe(
                [
                    {
                        "Date": p["payment_date"],
                        "Method": p["payment_method_description"],
                        "Amount ($)": p["payment_amount"],
                        "Status": p["payment_status"],
                        "Transaction reference": p["transaction_reference"] or "—",
                    }
                    for p in booking["payments"]
                ],
                width="stretch",
                hide_index=True,
            )

            # Tickets for this booking
            st.markdown("**Tickets**")
            st.dataframe(
                [{"Ticket number": t["ticket_code"], "Status": t["ticket_status"]} for t in booking["tickets"]],
                width="stretch",
                hide_index=True,
            )

            # Cancel button: only for CONFIRMED or PENDING bookings whose event hasn't started
            if booking["booking_status_code"] in SEAT_HOLDING_STATUSES and booking["is_upcoming"]:
                st.markdown("**Cancel this booking**")
                st.caption("Paid amounts are refunded to the original payment method and all tickets in this booking are cancelled.")
                sure = st.checkbox("Yes, I want to cancel this booking", key=f"cancel_confirm_{lookup_id}")
                if st.button("Cancel booking"):
                    if not sure:
                        st.warning("Please tick the confirmation box first.")
                    else:
                        refunded = cancel_booking(lookup_id, lookup_email)
                        if refunded is not None:
                            st.session_state["flash"] = (
                                f"Booking {lookup_id} was cancelled. "
                                + (f"${refunded:.2f} will be refunded." if refunded else "No payment had been made, so there is nothing to refund.")
                            )
                            st.rerun()
            else:
                st.info("This booking can no longer be cancelled.")

    st.stop()  # stop here so customers never reach the manager code below


# ===================== MANAGER AREA (everything below only runs when the manager is signed in) =====================
st.title("Event Booking System – Manager")
show_flash()

# Sidebar menu: choose a manager page
manager_page = st.sidebar.radio("Manager page", ["Event fill rates", "Confirm cash payments", "Edit tables"])


# Mark a box-office cash payment as paid in ONE transaction: payment COMPLETED (the trigger gives it a TXN- reference),
# booking CONFIRMED, tickets VALID. Returns the new transaction reference, or None on failure.
def confirm_cash_payment(payment_id):
    conn = get_connection()
    try:
        cur = conn.cursor()

        # Complete the payment (only if it is still a pending cash payment)
        cur.execute(
            "UPDATE payments SET payment_status = %s, payment_date = NOW() "
            "WHERE payment_id = %s AND payment_method_code = %s AND payment_status = %s "
            "RETURNING booking_id, transaction_reference",
            (PAYMENT_STATUS, payment_id, CASH_METHOD, CASH_PAYMENT_STATUS),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError("That payment is no longer pending.")
        booking_id, reference = row

        # Confirm the booking and activate its tickets
        cur.execute("UPDATE customer_bookings SET booking_status_code = %s WHERE booking_id = %s", (CONFIRMED_STATUS, booking_id))
        cur.execute("UPDATE tickets SET ticket_status = %s WHERE booking_id = %s", (TICKET_STATUS, booking_id))

        conn.commit()
        return reference or "(no reference)"
    except Exception as e:
        conn.rollback()
        st.error(f"Could not confirm the payment, nothing was changed: {e}")
        return None
    finally:
        conn.close()


# ===================== MANAGER: EVENT FILL RATES =====================
if manager_page == "Event fill rates":
    st.header("Event fill rates")
    st.caption("Seats used = CONFIRMED + PENDING bookings. Revenue = COMPLETED payments only.")
    fill = read_data(
        "SELECT e.event_id, e.event_name, e.event_start_date, v.venue_name, v.venue_seat_capacity, "
        "COALESCE(SUM(b.booking_seat_count) FILTER (WHERE b.booking_status_code = %s), 0) AS confirmed_seats, "
        "COALESCE(SUM(b.booking_seat_count) FILTER (WHERE b.booking_status_code = %s), 0) AS pending_seats, "
        "(SELECT COALESCE(SUM(p.payment_amount), 0) FROM payments p JOIN customer_bookings pb ON pb.booking_id = p.booking_id "
        " WHERE pb.event_id = e.event_id AND p.payment_status = %s) AS revenue "
        "FROM events e "
        "JOIN venues v ON v.venue_id = e.venue_id "
        "LEFT JOIN customer_bookings b ON b.event_id = e.event_id "
        "GROUP BY e.event_id, v.venue_name, v.venue_seat_capacity "
        "ORDER BY e.event_start_date",
        (CONFIRMED_STATUS, BOOKING_STATUS, PAYMENT_STATUS),
    )

    if not fill:
        st.info("There are no events yet.")
    else:
        # Work out seats used and % filled for each event
        rows = []
        for e in fill:
            used = e["confirmed_seats"] + e["pending_seats"]
            capacity = e["venue_seat_capacity"]
            rows.append({
                "Event": e["event_name"],
                "Starts": e["event_start_date"],
                "Venue": e["venue_name"],
                "Capacity": capacity,
                "Confirmed seats": e["confirmed_seats"],
                "Pending (cash) seats": e["pending_seats"],
                "Seats left": capacity - used,
                "% filled": round(used / capacity * 100, 1) if capacity else 0.0,
                "Revenue ($)": float(e["revenue"]),
            })

        # Totals across all events
        total_capacity = sum(r["Capacity"] for r in rows)
        total_used = sum(r["Confirmed seats"] + r["Pending (cash) seats"] for r in rows)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Events", len(rows))
        c2.metric("Seats used", f"{total_used:,} of {total_capacity:,}")
        c3.metric("Sold out", sum(1 for r in rows if r["Seats left"] <= 0))
        c4.metric("Revenue", f"${sum(r['Revenue ($)'] for r in rows):,.2f}")

        # One row per event, with a bar showing how full it is
        st.dataframe(
            rows,
            width="stretch",
            hide_index=True,
            column_config={
                "% filled": st.column_config.ProgressColumn("% filled", min_value=0, max_value=100, format="%.1f%%"),
                "Revenue ($)": st.column_config.NumberColumn("Revenue ($)", format="$%.2f"),
            },
        )
    st.stop()


# ===================== MANAGER: CONFIRM CASH PAYMENTS =====================
if manager_page == "Confirm cash payments":
    st.header("Confirm cash payments")
    st.caption("Pending box-office payments. Confirming one completes the payment, confirms the booking and activates its tickets.")
    pending = read_data(
        "SELECT p.payment_id, p.booking_id, c.customer_name, c.customer_email, e.event_name, e.event_start_date, "
        "b.booking_seat_count, p.payment_amount, p.payment_date "
        "FROM payments p "
        "JOIN customer_bookings b ON b.booking_id = p.booking_id "
        "JOIN customers c ON c.customer_id = b.customer_id "
        "JOIN events e ON e.event_id = b.event_id "
        "WHERE p.payment_method_code = %s AND p.payment_status = %s AND b.booking_status_code = %s "
        "ORDER BY e.event_start_date, p.payment_id",
        (CASH_METHOD, CASH_PAYMENT_STATUS, BOOKING_STATUS),
    )

    if not pending:
        st.info("There are no cash payments waiting to be confirmed.")
    else:
        # List of everything waiting to be paid at the box office
        st.dataframe(
            [
                {
                    "Payment ID": p["payment_id"],
                    "Booking ID": p["booking_id"],
                    "Customer": p["customer_name"],
                    "Email": p["customer_email"],
                    "Event": p["event_name"],
                    "Event date": p["event_start_date"],
                    "Seats": p["booking_seat_count"],
                    "Amount due ($)": p["payment_amount"],
                    "Booked on": p["payment_date"],
                }
                for p in pending
            ],
            width="stretch",
            hide_index=True,
        )

        # Pick the payment the customer just paid and confirm it
        by_id = {p["payment_id"]: p for p in pending}
        pid = st.selectbox(
            "Payment received for",
            list(by_id.keys()),
            format_func=lambda i: f"Booking {by_id[i]['booking_id']} – {by_id[i]['customer_name']} – {by_id[i]['event_name']} – ${by_id[i]['payment_amount']:.2f}",
        )
        if st.button("Confirm cash received", type="primary"):
            reference = confirm_cash_payment(pid)
            if reference:
                st.session_state["flash"] = f"Payment confirmed for booking {by_id[pid]['booking_id']}. Transaction reference: {reference}"
                st.rerun()
    st.stop()


# ===================== MANAGER: EDIT TABLES =====================
# Tables that use the generic insert/update/delete screens (Customers and Bookings have their own custom screens)
GENERIC_TABLES = {
    "Events": "events",
    "Venues": "venues",
    "Event organizers": "event_organizers",
    "Event types": "ref_event_types",
    "Booking statuses": "ref_booking_statuses",
    "Payment methods": "ref_payment_methods",
    "Payments": "payments",
    "Tickets": "tickets",
}

# Sidebar menu: choose which of the 10 tables to work with and which action to perform
table = st.sidebar.radio("Table", ["Customers", "Bookings"] + list(GENERIC_TABLES.keys()))
action = st.sidebar.radio("Action", ["Insert", "Update", "Delete"])

# Load the dropdown choices from the lookup tables and the other tables
payment_methods = [r["payment_method_code"] for r in read_data("SELECT payment_method_code FROM ref_payment_methods ORDER BY payment_method_code")]
statuses = [r["booking_status_code"] for r in read_data("SELECT booking_status_code FROM ref_booking_statuses ORDER BY booking_status_code")]
customers = read_data("SELECT * FROM customers ORDER BY customer_id")
events = read_data("SELECT * FROM events ORDER BY event_id")
bookings = read_data("SELECT * FROM customer_bookings ORDER BY booking_id")

# Build lookup dictionaries so dropdowns show names instead of just ID numbers
customer_names = {c["customer_id"]: c["customer_name"] for c in customers}
event_names = {e["event_id"]: e["event_name"] for e in events}
customer_by_id = {c["customer_id"]: c for c in customers}
booking_by_id = {b["booking_id"]: b for b in bookings}


# ---------- Helpers for the generic table screens ----------

# Ask the database which columns a table has, and which of them make up the primary key
def get_table_info(table_name):
    cols = read_data(
        "SELECT column_name, data_type, is_nullable, column_default, is_identity "
        "FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s "
        "ORDER BY ordinal_position",
        (table_name,),
    )
    pk_rows = read_data(
        "SELECT kcu.column_name FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema "
        "WHERE tc.table_schema = 'public' AND tc.table_name = %s AND tc.constraint_type = 'PRIMARY KEY' "
        "ORDER BY kcu.ordinal_position",
        (table_name,),
    )
    return cols, [p["column_name"] for p in pk_rows]


# True if the database fills this column in by itself (e.g. auto-numbered IDs), so the form should skip it
def is_auto(col):
    default = col["column_default"] or ""
    return col["is_identity"] == "YES" or "nextval" in default


# Draw the right kind of input box for a column, based on its data type
def field_input(col, key, value=None):
    name = col["column_name"]
    dtype = col["data_type"]
    if dtype in ("integer", "smallint", "bigint"):
        return st.number_input(name, value=int(value) if value is not None else 0, step=1, key=key)
    if dtype in ("numeric", "double precision", "real"):
        return st.number_input(name, value=float(value) if value is not None else 0.0, key=key)
    if dtype == "date":
        return st.date_input(name, value=value or date.today(), key=key)
    # text, varchar and timestamp columns use a text box (timestamps as YYYY-MM-DD HH:MM:SS)
    return st.text_input(name, value="" if value is None else str(value), key=key)


# Turn an empty text box into NULL for columns that allow NULL (e.g. a blank transaction reference)
def clean_value(value, col):
    if value == "" and col["is_nullable"] == "YES":
        return None
    return value


# Generic insert / update / delete screen that works for any table in GENERIC_TABLES
def generic_crud(table_name, action):
    cols, pk = get_table_info(table_name)
    col_by_name = {c["column_name"]: c for c in cols}
    rows = read_data(f"SELECT * FROM {table_name} ORDER BY 1")

    # INSERT: one input per column (auto-numbered ID columns are skipped)
    if action == "Insert":
        st.subheader("Add a new row")
        values = {}
        for c in cols:
            if not is_auto(c):
                values[c["column_name"]] = field_input(c, key=f"{table_name}_insert_{c['column_name']}")

        # Save the new row when the button is clicked
        if st.button("Add row"):
            names = list(values.keys())
            sql = f"INSERT INTO {table_name} ({', '.join(names)}) VALUES ({', '.join(['%s'] * len(names))})"
            params = tuple(clean_value(values[n], col_by_name[n]) for n in names)
            if write_data(sql, params):
                st.success("Row added.")

    # UPDATE: pick an existing row, edit the non-key columns, and save
    elif action == "Update":
        st.subheader("Update a row")
        if not rows:
            st.info("This table has no rows yet.")
        else:
            labels = {i: " - ".join(str(v) for v in list(r.values())[:2]) for i, r in enumerate(rows)}
            idx = st.selectbox("Select row", list(labels.keys()), format_func=lambda i: labels[i], key=f"{table_name}_update_select")
            current = rows[idx]
            values = {}
            for c in cols:
                n = c["column_name"]
                if n not in pk:  # primary key values are not edited
                    values[n] = field_input(c, key=f"{table_name}_update_{idx}_{n}", value=current[n])

            # Save the changes when the button is clicked
            if st.button("Update row"):
                set_part = ", ".join(f"{n}=%s" for n in values)
                where_part = " AND ".join(f"{p}=%s" for p in pk)
                sql = f"UPDATE {table_name} SET {set_part} WHERE {where_part}"
                params = tuple(clean_value(values[n], col_by_name[n]) for n in values) + tuple(current[p] for p in pk)
                if write_data(sql, params):
                    st.success("Row updated.")

    # DELETE: pick a row and require a confirmation checkbox before deleting
    elif action == "Delete":
        st.subheader("Delete a row")
        if not rows:
            st.info("This table has no rows yet.")
        else:
            labels = {i: " - ".join(str(v) for v in list(r.values())[:2]) for i, r in enumerate(rows)}
            idx = st.selectbox("Select row", list(labels.keys()), format_func=lambda i: labels[i], key=f"{table_name}_delete_select")
            confirm = st.checkbox("Yes, I am sure I want to delete this row", key=f"{table_name}_delete_confirm")

            # Only delete if the confirmation box is ticked
            if st.button("Delete row"):
                if not confirm:
                    st.warning("Please tick the confirmation box first.")
                else:
                    where_part = " AND ".join(f"{p}=%s" for p in pk)
                    if write_data(f"DELETE FROM {table_name} WHERE {where_part}", tuple(rows[idx][p] for p in pk)):
                        st.success("Row deleted.")

    # Show the current contents of the table (reloaded so changes appear right away)
    st.subheader(f"All rows in {table_name}")
    st.dataframe(read_data(f"SELECT * FROM {table_name} ORDER BY 1"), width="stretch")


# ===================== MANAGER: CUSTOMERS =====================
if table == "Customers":
    st.header("Customers")

    # INSERT: form that adds a new customer row
    if action == "Insert":
        st.subheader("Add a new customer")
        name = st.text_input("Name")
        phone = st.text_input("Phone")
        email = st.text_input("Email")
        address = st.text_input("Address")
        method = st.selectbox("Payment method", payment_methods)

        # Save the new customer when the button is clicked
        if st.button("Add customer"):
            if name.strip() == "":
                st.warning("Name is required.")
            else:
                ok = write_data(
                    "INSERT INTO customers (customer_name, customer_phone, customer_email, customer_address, customer_payment_method) VALUES (%s, %s, %s, %s, %s)",
                    (name, phone, email, address, method),
                )
                if ok:
                    st.success("Customer added.")

    # UPDATE: pick an existing customer, edit the fields, and save
    elif action == "Update":
        st.subheader("Update a customer")
        if not customers:
            st.info("There are no customers yet.")
        else:
            cid = st.selectbox("Select customer", list(customer_names.keys()), format_func=lambda i: f"{i} - {customer_names[i]}")
            current = customer_by_id[cid]
            name = st.text_input("Name", value=current["customer_name"], key=f"cust_name_{cid}")
            phone = st.text_input("Phone", value=current["customer_phone"] or "", key=f"cust_phone_{cid}")
            email = st.text_input("Email", value=current["customer_email"] or "", key=f"cust_email_{cid}")
            address = st.text_input("Address", value=current["customer_address"] or "", key=f"cust_address_{cid}")
            method_index = payment_methods.index(current["customer_payment_method"]) if current["customer_payment_method"] in payment_methods else 0
            method = st.selectbox("Payment method", payment_methods, index=method_index, key=f"cust_method_{cid}")

            # Save the changes when the button is clicked
            if st.button("Update customer"):
                ok = write_data(
                    "UPDATE customers SET customer_name=%s, customer_phone=%s, customer_email=%s, customer_address=%s, customer_payment_method=%s WHERE customer_id=%s",
                    (name, phone, email, address, method, cid),
                )
                if ok:
                    st.success("Customer updated.")

    # DELETE: pick a customer and require a confirmation checkbox before deleting
    elif action == "Delete":
        st.subheader("Delete a customer")
        if not customers:
            st.info("There are no customers yet.")
        else:
            cid = st.selectbox("Select customer", list(customer_names.keys()), format_func=lambda i: f"{i} - {customer_names[i]}")
            confirm = st.checkbox("Yes, I am sure I want to delete this customer")

            # Only delete if the confirmation box is ticked
            if st.button("Delete customer"):
                if not confirm:
                    st.warning("Please tick the confirmation box first.")
                else:
                    ok = write_data("DELETE FROM customers WHERE customer_id=%s", (cid,))
                    if ok:
                        st.success("Customer deleted.")

    # Show the current contents of the customers table (reloaded so changes appear right away)
    st.subheader("All customers")
    st.dataframe(read_data("SELECT * FROM customers ORDER BY customer_id"), width="stretch")


# ===================== MANAGER: BOOKINGS =====================
if table == "Bookings":
    st.header("Bookings")

    # INSERT: form that adds a new booking row
    if action == "Insert":
        st.subheader("Add a new booking")
        if not customers or not events:
            st.info("Add at least one customer and one event first.")
        else:
            cid = st.selectbox("Customer", list(customer_names.keys()), format_func=lambda i: f"{i} - {customer_names[i]}")
            eid = st.selectbox("Event", list(event_names.keys()), format_func=lambda i: f"{i} - {event_names[i]}")
            status = st.selectbox("Status", statuses)
            seats = st.number_input("Number of seats", min_value=1, value=1, step=1)

            # Save the new booking (today's date and the event's start time are filled in automatically)
            if st.button("Add booking"):
                ok = write_data(
                    "INSERT INTO customer_bookings (customer_id, event_id, booking_status_code, event_datetime, booking_made_date, booking_seat_count) "
                    "VALUES (%s, %s, %s, (SELECT event_start_date FROM events WHERE event_id = %s), CURRENT_DATE, %s)",
                    (cid, eid, status, eid, int(seats)),
                )
                if ok:
                    st.success("Booking added.")

    # UPDATE: pick an existing booking, change its customer, event, status or seat count, and save
    elif action == "Update":
        st.subheader("Update a booking")
        if not bookings:
            st.info("There are no bookings yet.")
        else:
            bid = st.selectbox("Select booking", list(booking_by_id.keys()))
            current = booking_by_id[bid]
            customer_ids = list(customer_names.keys())
            event_ids = list(event_names.keys())
            cid = st.selectbox(
                "Customer", customer_ids, index=customer_ids.index(current["customer_id"]) if current["customer_id"] in customer_ids else 0,
                format_func=lambda i: f"{i} - {customer_names[i]}", key=f"booking_customer_{bid}",
            )
            eid = st.selectbox(
                "Event", event_ids, index=event_ids.index(current["event_id"]) if current["event_id"] in event_ids else 0,
                format_func=lambda i: f"{i} - {event_names[i]}", key=f"booking_event_{bid}",
            )
            status_index = statuses.index(current["booking_status_code"]) if current["booking_status_code"] in statuses else 0
            status = st.selectbox("Status", statuses, index=status_index, key=f"booking_status_{bid}")
            seats = st.number_input("Number of seats", min_value=1, value=int(current["booking_seat_count"]), step=1, key=f"booking_seats_{bid}")

            # Save the changes (the event date is refreshed from the chosen event)
            if st.button("Update booking"):
                ok = write_data(
                    "UPDATE customer_bookings SET customer_id=%s, event_id=%s, booking_status_code=%s, booking_seat_count=%s, "
                    "event_datetime=(SELECT event_start_date FROM events WHERE event_id = %s) WHERE booking_id=%s",
                    (cid, eid, status, int(seats), eid, bid),
                )
                if ok:
                    st.success("Booking updated.")

    # DELETE: pick a booking and require a confirmation checkbox before deleting
    elif action == "Delete":
        st.subheader("Delete a booking")
        if not bookings:
            st.info("There are no bookings yet.")
        else:
            bid = st.selectbox("Select booking", list(booking_by_id.keys()))
            confirm = st.checkbox("Yes, I am sure I want to delete this booking")

            # Only delete if the confirmation box is ticked
            if st.button("Delete booking"):
                if not confirm:
                    st.warning("Please tick the confirmation box first.")
                else:
                    ok = write_data("DELETE FROM customer_bookings WHERE booking_id=%s", (bid,))
                    if ok:
                        st.success("Booking deleted.")

    # Show the current contents of the bookings table (reloaded so changes appear right away)
    st.subheader("All bookings")
    st.dataframe(read_data("SELECT * FROM customer_bookings ORDER BY booking_id"), width="stretch")


# ===================== MANAGER: ALL OTHER TABLES (generic screens) =====================
if table in GENERIC_TABLES:
    st.header(table)
    generic_crud(GENERIC_TABLES[table], action)
