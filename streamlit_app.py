import streamlit as st
import psycopg2
from datetime import date

# ===================== LOGIN ACCOUNTS =====================
# Each account has a password and a role ("user" = read-only Events/Venues, "manager" = full access)
USERS = {
    "user@example.com": {"password": "COMP4522", "role": "user"},
    "manager@example.com": {"password": "COMP4522_MANAGER", "role": "manager"},
}


# Connect to the Supabase Postgres database using the connection string saved in .streamlit/secrets.toml
def get_connection():
    return psycopg2.connect(st.secrets["DB_URL"])


# Run a SELECT query and return the rows as a list of dictionaries (so Streamlit can show them as a table)
def read_data(sql, params=None):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(sql, params)
    columns = [col[0] for col in cur.description]
    rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


# Run an INSERT / UPDATE / DELETE query, save the change, and show an error message if the database rejects it
def write_data(sql, params):
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        st.error(f"Database error: {e}")
        return False


# ===================== LOGIN PAGE =====================
# Remember who is logged in between reruns of the script
if "role" not in st.session_state:
    st.session_state["role"] = None
    st.session_state["email"] = None


# Show the login form and check the email/password against the USERS dictionary
def login_page():
    st.title("Event Booking System")
    st.subheader("Login")
    with st.form("login_form"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Log in")

    if submitted:
        account = USERS.get(email.strip().lower())
        if account and account["password"] == password:
            st.session_state["role"] = account["role"]
            st.session_state["email"] = email.strip().lower()
            st.rerun()
        else:
            st.error("Invalid email or password.")


# Nothing else is shown until the person is logged in
if st.session_state["role"] is None:
    login_page()
    st.stop()

# Logged-in info and logout button in the sidebar
st.sidebar.write(f"Logged in as: {st.session_state['email']}")
if st.sidebar.button("Log out"):
    st.session_state["role"] = None
    st.session_state["email"] = None
    st.rerun()

# Set the page title shown at the top of the app
st.title("Event Booking System")


# ===================== BOOKING HELPER (used by the regular user) =====================
# Status values written by the booking form. Change these if your tables use different codes.
BOOKING_STATUS = "PENDING"  # the database trigger sets it to CONFIRMED when a payment is inserted
PAYMENT_STATUS = "COMPLETED"  # the trigger trg_payment_confirm_booking fires on this value
CASH_PAYMENT_STATUS = "PENDING"  # cash is paid at the box office, so the booking stays PENDING until then
TICKET_STATUS = "VALID"


# Create the customer (if new), booking, payment and tickets in ONE transaction.
# If any step fails, nothing is saved. Returns the booking id and ticket codes, or None on failure.
def create_booking(name, phone, email, address, event_id, ticket_count, method, amount, reference):
    conn = get_connection()
    try:
        cur = conn.cursor()

        # Reuse the customer if the email already exists, otherwise create a new customer
        cur.execute("SELECT customer_id FROM customers WHERE LOWER(customer_email) = LOWER(%s)", (email,))
        row = cur.fetchone()
        if row:
            customer_id = row[0]
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

        # Payment record (the reference is optional and must be unique if given).
        # Cash is paid later at the box office, so it is saved as PENDING and the trigger does not confirm the booking.
        payment_status = CASH_PAYMENT_STATUS if method == "CASH" else PAYMENT_STATUS
        cur.execute(
            "INSERT INTO payments (booking_id, payment_method_code, payment_amount, payment_date, payment_status, transaction_reference) "
            "VALUES (%s, %s, %s, NOW(), %s, %s)",
            (booking_id, method, amount, payment_status, reference or None),
        )

        # Read back the booking status (the trigger on payments may have changed PENDING to CONFIRMED)
        cur.execute("SELECT booking_status_code FROM customer_bookings WHERE booking_id = %s", (booking_id,))
        final_status = cur.fetchone()[0]

        # One ticket per seat; the code is built from the event id, booking id and ticket number
        ticket_codes = []
        for n in range(1, ticket_count + 1):
            code = f"TKT-E{event_id:02d}-B{booking_id:03d}-{n:02d}"  # same pattern as the existing tickets
            cur.execute(
                "INSERT INTO tickets (booking_id, ticket_code, ticket_status, ticket_issue_date) VALUES (%s, %s, %s, NOW())",
                (booking_id, code, TICKET_STATUS),
            )
            ticket_codes.append(code)

        conn.commit()
        return {"booking_id": booking_id, "ticket_codes": ticket_codes, "status": final_status}
    except Exception as e:
        conn.rollback()
        st.error(f"Booking failed, nothing was saved: {e}")
        return None
    finally:
        conn.close()


# ===================== REGULAR USER (view Events and Venues, make a booking) =====================
if st.session_state["role"] == "user":
    choice = st.sidebar.radio("Page", ["Events", "Venues", "Make a booking"])
    st.header(choice)

    # View-only pages
    if choice == "Events":
        st.dataframe(read_data("SELECT * FROM events ORDER BY event_id"), use_container_width=True)
    elif choice == "Venues":
        st.dataframe(read_data("SELECT * FROM venues ORDER BY venue_id"), use_container_width=True)

    # Booking page: confirmation screen after a successful booking, otherwise the single booking form
    else:
        confirmation = st.session_state.get("confirmation")

        if confirmation:
            if confirmation["status"] == "CONFIRMED":
                st.success("Your booking is confirmed!")
            else:
                st.info(f"Your booking was received and is awaiting payment. Status: {confirmation['status']}")
            st.subheader(f"Booking ID: {confirmation['booking_id']}")
            st.write(f"**Event:** {confirmation['event']}")
            st.write(f"**Name:** {confirmation['name']}")
            st.write(f"**Email:** {confirmation['email']}")
            st.write(f"**Tickets:** {len(confirmation['ticket_codes'])}")
            st.write(f"**Payment:** {confirmation['amount']:.2f} by {confirmation['method']}")
            st.write("**Your ticket code(s):**")
            for code in confirmation["ticket_codes"]:
                st.code(code, language=None)
            if st.button("Make another booking"):
                st.session_state["confirmation"] = None
                st.rerun()
        else:
            user_events = read_data("SELECT event_id, event_name, event_start_date FROM events ORDER BY event_id")
            user_event_labels = {e["event_id"]: f"{e['event_name']} ({e['event_start_date']})" for e in user_events}
            user_methods = {r["payment_method_code"]: r["payment_method_description"] for r in read_data("SELECT payment_method_code, payment_method_description FROM ref_payment_methods ORDER BY payment_method_code")}

            if not user_events:
                st.info("There are no events available to book yet.")
            else:
                with st.form("booking_form"):
                    st.markdown("**Your details**")
                    name = st.text_input("Name")
                    phone = st.text_input("Phone")
                    email = st.text_input("Email")
                    address = st.text_input("Address")

                    st.markdown("**Booking**")
                    eid = st.selectbox("Event", list(user_event_labels.keys()), format_func=lambda i: user_event_labels[i])
                    tickets = st.number_input("Number of tickets", min_value=1, value=1, step=1)

                    st.markdown("**Payment**")
                    method = st.selectbox("Payment type", list(user_methods.keys()), format_func=lambda m: user_methods[m])
                    amount = st.number_input("Payment amount", min_value=0.01, value=10.00, step=1.00, format="%.2f")
                    reference = st.text_input("Transaction reference (optional)")
                    submitted = st.form_submit_button("Book now")

                if submitted:
                    if name.strip() == "" or phone.strip() == "" or email.strip() == "":
                        st.warning("Name, phone and email are required.")
                    else:
                        result = create_booking(
                            name.strip(), phone.strip(), email.strip(), address.strip(),
                            eid, int(tickets), method, float(amount), reference.strip(),
                        )
                        if result:
                            st.session_state["confirmation"] = {
                                "booking_id": result["booking_id"],
                                "ticket_codes": result["ticket_codes"],
                                "status": result["status"],
                                "event": user_event_labels[eid],
                                "name": name.strip(),
                                "email": email.strip(),
                                "method": user_methods[method],
                                "amount": float(amount),
                            }
                            st.rerun()

    st.stop()  # stop here so a regular user never reaches the manager code below


# ===================== MANAGER (everything below only runs for the manager) =====================

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

# Sidebar menu: choose which table to work with and which action to perform
table = st.sidebar.radio("Table", ["Customers", "Bookings"] + list(GENERIC_TABLES.keys()))
action = st.sidebar.radio("Action", ["Insert", "Update", "Delete"])

# Load the dropdown choices from the lookup tables and the other tables (no JOINs are used in the app)
payment_methods = [row["payment_method_code"] for row in read_data("SELECT payment_method_code FROM ref_payment_methods ORDER BY payment_method_code")]
statuses = ["CONFIRMED", "PENDING", "CANCELLED", "EXPIRED"]  # must match the codes in ref_booking_statuses
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


# Turn an empty text box into NULL for columns that allow NULL
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

            if st.button("Delete row"):
                if not confirm:
                    st.warning("Please tick the confirmation box first.")
                else:
                    where_part = " AND ".join(f"{p}=%s" for p in pk)
                    if write_data(f"DELETE FROM {table_name} WHERE {where_part}", tuple(rows[idx][p] for p in pk)):
                        st.success("Row deleted.")

    # Show the current contents of the table (reloaded so changes appear right away)
    st.subheader(f"All rows in {table_name}")
    st.dataframe(read_data(f"SELECT * FROM {table_name} ORDER BY 1"), use_container_width=True)


# ===================== CUSTOMERS =====================
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
            if name == "":
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
        cid = st.selectbox("Select customer", list(customer_names.keys()), format_func=lambda i: f"{i} - {customer_names[i]}")
        current = customer_by_id[cid]
        name = st.text_input("Name", value=current["customer_name"])
        phone = st.text_input("Phone", value=current["customer_phone"] or "")
        email = st.text_input("Email", value=current["customer_email"] or "")
        address = st.text_input("Address", value=current["customer_address"] or "")
        method_index = payment_methods.index(current["customer_payment_method"]) if current["customer_payment_method"] in payment_methods else 0
        method = st.selectbox("Payment method", payment_methods, index=method_index)

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
    st.dataframe(read_data("SELECT * FROM customers ORDER BY customer_id"), use_container_width=True)


# ===================== BOOKINGS =====================
if table == "Bookings":
    st.header("Bookings")

    # INSERT: form that adds a new booking row
    if action == "Insert":
        st.subheader("Add a new booking")
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

    # UPDATE: pick an existing booking, change its status or seat count, and save
    elif action == "Update":
        st.subheader("Update a booking")
        bid = st.selectbox("Select booking", list(booking_by_id.keys()))
        current = booking_by_id[bid]
        status_index = statuses.index(current["booking_status_code"]) if current["booking_status_code"] in statuses else 0
        status = st.selectbox("Status", statuses, index=status_index)
        seats = st.number_input("Number of seats", min_value=1, value=int(current["booking_seat_count"]), step=1)

        # Save the changes when the button is clicked
        if st.button("Update booking"):
            ok = write_data(
                "UPDATE customer_bookings SET booking_status_code=%s, booking_seat_count=%s WHERE booking_id=%s",
                (status, int(seats), bid),
            )
            if ok:
                st.success("Booking updated.")

    # DELETE: pick a booking and require a confirmation checkbox before deleting
    elif action == "Delete":
        st.subheader("Delete a booking")
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
    st.dataframe(read_data("SELECT * FROM customer_bookings ORDER BY booking_id"), use_container_width=True)


# ===================== ALL OTHER TABLES (generic screens) =====================
if table in GENERIC_TABLES:
    st.header(table)
    generic_crud(GENERIC_TABLES[table], action)
